from astrbot.api.message_components import Image, Plain, File
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

import asyncio
import os
import glob
import random
import yaml
import re
import json
import traceback
import py7zr
import shutil
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
import time
import concurrent.futures
from threading import Lock

import jmcomic
from jmcomic import JmMagicConstants

# 添加自定义解析函数用于处理jmcomic库无法解析的情况
def extract_title_from_html(html_content: str) -> str:
    """从HTML内容中提取标题的多种尝试方法"""
    patterns = [
        r"<h1[^>]*>([^<]+)</h1>",
        r"<title>([^<]+)</title>",
        r'name:\s*[\'"]([^\'"]+)[\'"]',
        r'"name":\s*"([^"]+)"',
        r'data-title=[\'"]([^\'"]+)[\'"]',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, html_content)
        if matches:
            title = matches[0].strip()
            logger.info(f"已使用备用解析方法找到标题: {title}")
            return title

    return "未知标题"

def validate_comic_id(comic_id: str) -> bool:
    """验证漫画ID格式，防止路径遍历"""
    if not re.match(r"^\d+$", comic_id):
        return False
    if len(comic_id) > 10:
        return False
    return True

def validate_domain(domain: str) -> bool:
    """验证域名格式"""
    pattern = r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$"
    if not re.match(pattern, domain):
        return False
    if len(domain) > 253:
        return False
    blocked_domains = ["localhost", "127.0.0.1", "0.0.0.0"]
    return domain not in blocked_domains

def handle_download_error(error: Exception, context: str) -> str:
    """统一的错误处理"""
    error_msg = str(error)

    if "timeout" in error_msg.lower():
        return f"{context}超时，请检查网络连接或稍后重试"
    elif "connection" in error_msg.lower():
        return f"{context}连接失败，请检查网络或代理设置"
    elif "文本没有匹配上字段" in error_msg:
        return f"{context}失败：网站结构可能已更改，请使用 /jmdomain update 更新域名"
    elif "permission" in error_msg.lower() or "access" in error_msg.lower():
        return f"{context}失败：文件权限错误，请检查存储目录权限"
    elif "space" in error_msg.lower() or "disk" in error_msg.lower():
        return f"{context}失败：存储空间不足，请清理磁盘空间"
    else:
        logger.error(f"{context}未知错误: {error_msg}", exc_info=True)
        return f"{context}失败：{error_msg[:100]}"

# 使用数据类来管理配置
@dataclass
class CosmosConfig:
    """Cosmos插件配置类"""
    domain_list: List[str]
    proxy: Optional[str]
    avs_cookie: str
    max_threads: int
    debug_mode: bool
    show_cover: bool
    custom_password: str  # 新增自定义密码字段

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "CosmosConfig":
        """从字典创建配置对象"""
        return cls(
            domain_list=config_dict.get(
                "domain_list", ["18comic.vip", "jm365.xyz", "18comic.org"]
            ),
            proxy=config_dict.get("proxy"),
            avs_cookie=config_dict.get("avs_cookie", ""),
            max_threads=config_dict.get("max_threads", 10),
            debug_mode=config_dict.get("debug_mode", False),
            show_cover=config_dict.get("show_cover", True),
            custom_password=config_dict.get("custom_password", "")
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "domain_list": self.domain_list,
            "proxy": self.proxy,
            "avs_cookie": self.avs_cookie,
            "max_threads": self.max_threads,
            "debug_mode": self.debug_mode,
            "show_cover": self.show_cover,
            "custom_password": self.custom_password
        }

class ResourceManager:
    """资源管理器，管理文件路径和创建必要的目录"""

    def __init__(self, plugin_name: str):
        self.base_dir = StarTools.get_data_dir(plugin_name)
        # 目录结构
        self.downloads_dir = os.path.join(self.base_dir, "downloads")
        self.archives_dir = os.path.join(self.base_dir, "archives") # 改为 archives
        self.logs_dir = os.path.join(self.base_dir, "logs")
        self.temp_dir = os.path.join(self.base_dir, "temp")
        self.covers_dir = os.path.join(self.base_dir, "covers")

        self.max_storage_size = 2 * 1024 * 1024 * 1024  # 2GB限制
        self.max_file_age_days = 30

        # 创建必要的目录
        for dir_path in [
            self.downloads_dir,
            self.archives_dir,
            self.logs_dir,
            self.temp_dir,
            self.covers_dir,
        ]:
            if not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)

    def check_storage_space(self) -> tuple[bool, int]:
        total_size = 0
        try:
            for root, dirs, files in os.walk(self.base_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if os.path.exists(file_path):
                        total_size += os.path.getsize(file_path)
        except Exception as e:
            logger.error(f"计算存储空间时出错: {str(e)}")
            return False, 0

        return total_size < self.max_storage_size, total_size

    def cleanup_old_files(self) -> int:
        cutoff_time = time.time() - (self.max_file_age_days * 24 * 60 * 60)
        cleaned_count = 0
        try:
            for root, dirs, files in os.walk(self.base_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if os.path.exists(file_path):
                        if os.path.getmtime(file_path) < cutoff_time:
                            try:
                                os.remove(file_path)
                                cleaned_count += 1
                            except Exception:
                                pass
        except Exception:
            pass
        return cleaned_count

    def get_storage_info(self) -> dict:
        has_space, total_size = self.check_storage_space()
        return {
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "max_size_mb": round(self.max_storage_size / (1024 * 1024), 2),
            "has_space": has_space,
            "usage_percent": round((total_size / self.max_storage_size) * 100, 2),
        }

    def find_comic_folder(self, comic_id: str) -> str:
        # 尝试直接匹配ID
        id_path = os.path.join(self.downloads_dir, str(comic_id))
        if os.path.exists(id_path):
            return id_path
        return id_path

    def get_comic_folder(self, comic_id: str) -> str:
        return self.find_comic_folder(comic_id)

    def get_cover_path(self, comic_id: str) -> str:
        cover_path = os.path.join(self.covers_dir, f"{comic_id}.jpg")
        if os.path.exists(cover_path):
            file_size = os.path.getsize(cover_path)
            if file_size > 1000:
                return cover_path
            else:
                try:
                    os.remove(cover_path)
                except Exception:
                    pass
        return cover_path

    def get_archive_path(self, comic_id: str) -> str:
        """获取7z文件路径"""
        return os.path.join(self.archives_dir, f"{comic_id}.7z")

    def get_log_path(self, prefix: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.logs_dir, f"{prefix}_{timestamp}.txt")

    def list_comic_images(self, comic_id: str, limit: int = None) -> List[str]:
        comic_folder = self.get_comic_folder(comic_id)
        if not os.path.exists(comic_folder):
            return []
        
        image_files = []
        try:
            for root, dirs, files in os.walk(comic_folder):
                for file in files:
                     if file.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                         image_files.append(os.path.join(root, file))
            image_files.sort()
        except Exception:
            pass
        return image_files[:limit] if limit else image_files

    def clear_cover_cache(self):
        if os.path.exists(self.covers_dir):
            try:
                count = 0
                for file in os.listdir(self.covers_dir):
                    file_path = os.path.join(self.covers_dir, file)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        count += 1
                return count
            except Exception:
                return 0
        return 0

def compress_folder_to_7z(input_folder: str, output_7z: str, password: str, arcname: str):
    """
    压缩文件夹到7z，最大压缩，带密码加密
    input_folder: 图片所在文件夹
    output_7z: 输出文件路径
    password: 密码
    arcname: 压缩包内的根目录名
    """
    with py7zr.SevenZipFile(
        output_7z,
        mode="w",
        password=password,
        filters=[{"id": py7zr.FILTER_LZMA2, "preset": 9}]  # 最大压缩
    ) as archive:
        # 将整个文件夹写入压缩包，在压缩包内保持该文件夹名
        archive.writeall(input_folder, arcname=arcname)

class JMClientFactory:
    def __init__(self, config: CosmosConfig, resource_manager: ResourceManager):
        self.config = config
        self.resource_manager = resource_manager
        self.option = self._create_option()

    def _create_option(self):
        option_dict = {
            "client": {
                "impl": "html",
                "domain": self.config.domain_list,
                "retry_times": 5,
                "postman": {
                    "meta_data": {
                        "proxies": {"https": self.config.proxy} if self.config.proxy else None,
                        "cookies": {"AVS": self.config.avs_cookie},
                        "headers": {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
                            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                            "Referer": f"https://{self.config.domain_list[0]}/",
                        },
                    }
                },
            },
            "download": {
                "cache": True,
                "image": {"decode": True, "suffix": ".jpg"},
                "threading": {
                    "image": self.config.max_threads,
                    "photo": self.config.max_threads,
                },
            },
            "dir_rule": {"base_dir": self.resource_manager.downloads_dir},
            # 移除 img2pdf 插件，不再生成PDF
            "plugins": {}
        }
        yaml_str = yaml.safe_dump(option_dict, allow_unicode=True)
        return jmcomic.create_option_by_str(yaml_str)

    def create_client(self):
        return self.option.new_jm_client()

    def create_client_with_domain(self, domain: str):
        custom_option = jmcomic.JmOption.default()
        custom_option.client.domain = [domain]
        custom_option.client.postman.meta_data = {
            "proxies": {"https": self.config.proxy} if self.config.proxy else None,
            "cookies": {"AVS": self.config.avs_cookie},
        }
        return custom_option.new_jm_client()

    def update_option(self):
        self.option = self._create_option()

class ComicDownloader:
    def __init__(
        self,
        client_factory: JMClientFactory,
        resource_manager: ResourceManager,
        config: CosmosConfig,
    ):
        self.client_factory = client_factory
        self.resource_manager = resource_manager
        self.config = config
        self.downloading_comics: Set[str] = set()
        self.downloading_covers: Set[str] = set()
        self._download_lock = Lock()

        self._thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.config.max_threads, 20),
            thread_name_prefix="jm_download",
        )

    def __del__(self):
        if hasattr(self, "_thread_pool"):
            self._thread_pool.shutdown(wait=True)

    async def download_cover(self, album_id: str) -> Tuple[bool, str]:
        if album_id in self.downloading_covers:
            return False, "封面正在下载中"
        self.downloading_covers.add(album_id)
        try:
            client = self.client_factory.create_client()
            try:
                album = client.get_album_detail(album_id)
            except Exception as e:
                return False, f"获取详情失败: {str(e)}"

            if not album: return False, "漫画不存在"
            
            first_photo = album[0]
            photo = client.get_photo_detail(first_photo.photo_id, True)
            if not photo: return False, "章节为空"

            image = photo[0]
            cover_path = os.path.join(self.resource_manager.covers_dir, f"{album_id}.jpg")
            if os.path.exists(cover_path):
                os.remove(cover_path)

            # 确保下载目录存在，防止库报错
            os.makedirs(self.resource_manager.get_comic_folder(album_id), exist_ok=True)

            client.download_by_image_detail(image, cover_path)
            return True, cover_path
        except Exception as e:
            return False, str(e)
        finally:
            self.downloading_covers.discard(album_id)

    async def download_comic(self, album_id: str) -> Tuple[bool, Optional[str]]:
        with self._download_lock:
            if album_id in self.downloading_comics:
                return False, "该漫画正在下载中"
            self.downloading_comics.add(album_id)

        try:
            has_space, _ = self.resource_manager.check_storage_space()
            if not has_space:
                self.resource_manager.cleanup_old_files()
            
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._thread_pool, self._download_with_retry, album_id
            )
            return result
        except Exception as e:
            logger.error(f"下载调度失败: {str(e)}")
            return False, str(e)
        finally:
            with self._download_lock:
                self.downloading_comics.discard(album_id)

    def _download_with_retry(self, album_id: str) -> Tuple[bool, Optional[str]]:
        try:
            option = self.client_factory.option
            try:
                # 纯下载图片，不涉及PDF转换
                jmcomic.download_album(album_id, option)
                return True, None
            except Exception as e:
                # 简单重试逻辑
                logger.warning(f"下载失败，尝试备用域名: {str(e)}")
                if self.config.domain_list and len(self.config.domain_list) > 1:
                     for domain in self.config.domain_list[1:3]:
                         try:
                             backup_option = jmcomic.JmOption.default()
                             backup_option.client.domain = [domain]
                             backup_option.dir_rule.base_dir = option.dir_rule.base_dir
                             if self.config.proxy:
                                 backup_option.client.postman.meta_data = {"proxies": {"https": self.config.proxy}}
                             backup_option.client.postman.meta_data = backup_option.client.postman.meta_data or {}
                             if self.config.avs_cookie:
                                backup_option.client.postman.meta_data["cookies"] = {"AVS": self.config.avs_cookie}
                             
                             jmcomic.download_album(album_id, backup_option)
                             return True, None
                         except:
                             continue
                raise e
        except Exception as e:
            logger.error(f"下载失败: {str(e)}")
            return False, str(e)

    def get_total_pages(self, client, album) -> int:
        try:
            return sum(len(client.get_photo_detail(p.photo_id, False)) for p in album)
        except:
            return 0

    def preview_download_comic(self, client, comic_id: str, max_pages: int = 3) -> tuple[bool, str, list]:
        preview_dir = None
        downloaded_images = []
        try:
            album = client.get_album_detail(comic_id)
            preview_dir = os.path.join(self.resource_manager.base_dir, "preview_downloads", f"{comic_id}")
            os.makedirs(preview_dir, exist_ok=True)
            
            page_count = 0
            for episode in album:
                if page_count >= max_pages: break
                photo_detail = client.get_photo_detail(episode.photo_id, False)
                for photo in photo_detail:
                    if page_count >= max_pages: break
                    img_path = os.path.join(preview_dir, f"page_{page_count + 1:03d}.jpg")
                    client.download_by_image_detail(photo, img_path)
                    if os.path.exists(img_path):
                        downloaded_images.append(img_path)
                        page_count += 1
            return True, "成功", downloaded_images
        except Exception as e:
            return False, str(e), []

@register(
    "jmcomic_download",
    "Rentz",
    "全能型JM漫画下载与管理工具(修改自用版)",
    "1.0",
    "https://github.com/Rentz/astrbot_plugin_jm_cosmos_modify",
)
class JMCosmosPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.plugin_name = "jmcomic_download"
        self.base_path = os.path.realpath(os.path.dirname(__file__))
        self.resource_manager = ResourceManager(self.plugin_name)
        self.resource_manager.clear_cover_cache()

        # 配置加载逻辑
        self.astrbot_config_path = os.path.join(
            self.context.get_config().get("data_dir", "data"),
            "config",
            f"astrbot_plugin_{self.plugin_name}_config.json",
        )

        # 解析配置
        config_data = {}
        if config:
            config_data = config
        elif os.path.exists(self.astrbot_config_path):
            try:
                with open(self.astrbot_config_path, "r", encoding="utf-8-sig") as f:
                    config_data = json.load(f)
            except:
                pass
        
        # 处理配置字段
        domain_list = config_data.get("domain_list", ["18comic.vip", "jm365.xyz", "18comic.org"])
        if isinstance(domain_list, str): domain_list = domain_list.split(",")
        
        self.config = CosmosConfig(
            domain_list=domain_list,
            proxy=config_data.get("proxy"),
            avs_cookie=str(config_data.get("avs_cookie", "")),
            max_threads=int(config_data.get("max_threads", 10)),
            debug_mode=bool(config_data.get("debug_mode", False)),
            show_cover=bool(config_data.get("show_cover", True)),
            custom_password=str(config_data.get("custom_password", ""))
        )

        self.client_factory = JMClientFactory(self.config, self.resource_manager)
        self.downloader = ComicDownloader(
            self.client_factory, self.resource_manager, self.config
        )

    async def _build_album_message(self, client, album, album_id: str, cover_path: str) -> List:
        total_pages = self.downloader.get_total_pages(client, album)
        message = (
            f"📖: {album.title}\n"
            f"🆔: {album_id}\n"
            f"🏷️: {', '.join(album.tags[:5])}\n"
            f"📅: {getattr(album, 'pub_date', '未知')}\n"
            f"📃: {total_pages}"
        )
        if self.config.show_cover:
            return [Plain(text=message), Image.fromFileSystem(cover_path)]
        return [Plain(text=message)]
    
    def _save_debug_info(self, prefix: str, content: str) -> None:
        if not self.config.debug_mode: return
        try:
            log_path = self.resource_manager.get_log_path(prefix)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(content)
        except: pass

    @filter.command("jm")
    async def download_comic(self, event: AstrMessageEvent):
        """下载JM漫画并压缩加密 (7z)

        用法: /jm [漫画ID]
        """
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("请提供漫画ID，例如：/jm 12345")
            return

        comic_id = args[1]
        if not validate_comic_id(comic_id):
            yield event.plain_result("无效的漫画ID格式")
            return

        # 1. 检测 .7z 是否已存在
        archive_path = self.resource_manager.get_archive_path(comic_id)
        abs_archive_path = os.path.abspath(archive_path)
        
        # 密码逻辑
        pwd = self.config.custom_password.strip()
        is_custom_pwd = True
        if not pwd:
            pwd = f"jm{comic_id}"
            is_custom_pwd = False

        if os.path.exists(abs_archive_path):
            yield event.plain_result("压缩包已存在，直接发送...")
            if is_custom_pwd:
                yield event.plain_result(f"🔑 本压缩包使用自定义密码: {pwd}")
            else:
                yield event.plain_result(f"🔑 本压缩包密码为: {pwd}")
            
            # 发送逻辑
            yield event.chain_result([File(name=f"{comic_id}.7z", file=abs_archive_path)])
            return

        yield event.plain_result(f"开始下载漫画 {comic_id}，下载完成后将进行高压缩加密...")

        # 2. 下载漫画图片
        success, msg = await self.downloader.download_comic(comic_id)
        if not success:
            yield event.plain_result(f"下载失败: {msg}")
            return
        
        # 3. 压缩文件夹
        comic_folder = self.resource_manager.get_comic_folder(comic_id)
        if not os.path.exists(comic_folder) or not os.listdir(comic_folder):
            yield event.plain_result("下载看似成功但未找到图片文件夹")
            return

        try:
            yield event.plain_result("正在进行极限压缩与加密 (AES-256)...")
            # 阻塞执行压缩，防止并发IO问题
            await asyncio.to_thread(
                compress_folder_to_7z, 
                input_folder=comic_folder, 
                output_7z=abs_archive_path, 
                password=pwd,
                arcname=comic_id
            )
            
            # 4. 删除原图文件夹
            logger.info(f"压缩完成，正在删除原图文件夹: {comic_folder}")
            shutil.rmtree(comic_folder)

            if is_custom_pwd:
                yield event.plain_result(f"✅ 处理完成！\n🔑 解压密码(自定义): {pwd}")
            else:
                yield event.plain_result(f"✅ 处理完成！\n🔑 解压密码: {pwd}")

            # 5. 发送文件
            if os.path.exists(abs_archive_path):
                yield event.chain_result([File(name=f"{comic_id}.7z", file=abs_archive_path)])
            else:
                yield event.plain_result("压缩文件生成失败")

        except Exception as e:
            logger.error(f"压缩或清理失败: {str(e)}")
            yield event.plain_result(f"处理过程出错: {str(e)}")

    @filter.command("jm7z")
    async def check_archive_info(self, event: AstrMessageEvent):
        """查看7z压缩包信息
        用法: /jm7z [漫画ID]
        """
        args = event.message_str.strip().split()
        if len(args) < 2: return
        comic_id = args[1]
        archive_path = self.resource_manager.get_archive_path(comic_id)

        if not os.path.exists(archive_path):
            yield event.plain_result(f"未找到漫画 {comic_id} 的压缩包")
            return
        
        size_mb = os.path.getsize(archive_path) / (1024 * 1024)
        ctime = datetime.fromtimestamp(os.path.getctime(archive_path)).strftime("%Y-%m-%d %H:%M:%S")
        
        # 获取当前配置的密码提示
        pwd_hint = self.config.custom_password if self.config.custom_password else f"jm{comic_id}"
        
        msg = (
            f"📦 压缩包信息\n"
            f"🆔 ID: {comic_id}\n"
            f"💾 大小: {size_mb:.2f} MB\n"
            f"📅 创建: {ctime}\n"
            f"🔑 当前配置对应的解压密码: {pwd_hint}\n"
            f"(注意：如果文件是在修改密码配置前生成的，密码可能不同)"
        )
        yield event.plain_result(msg)

    @filter.command("jmconfig")
    async def config_plugin(self, event: AstrMessageEvent):
        """配置JM漫画插件"""
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result(
                "用法:\n/jmconfig password [密码] - 设置自定义密码(空则使用默认)\n/jmconfig proxy [URL] - 设置代理\n/jmconfig noproxy - 清除代理\n/jmconfig threads [N] - 线程数\n/jmconfig info - 查看配置"
            )
            return

        action = args[1].lower()

        if action == "password":
            if len(args) >= 3:
                pwd = " ".join(args[2:]) # 允许密码包含空格
                self.config.custom_password = pwd
                if self._update_astrbot_config("custom_password", pwd):
                    yield event.plain_result(f"已设置自定义密码为: {pwd}")
                else:
                    yield event.plain_result("保存失败")
            else:
                # 清空密码
                self.config.custom_password = ""
                if self._update_astrbot_config("custom_password", ""):
                    yield event.plain_result("已清除自定义密码，将使用默认格式 jm{ID}")
                else:
                    yield event.plain_result("保存失败")
        
        elif action == "info":
            pwd_status = f"自定义 ({self.config.custom_password})" if self.config.custom_password else "自动 (jm{ID})"
            info_msg = (
                f"当前配置:\n"
                f"代理: {self.config.proxy or '无'}\n"
                f"线程: {self.config.max_threads}\n"
                f"密码模式: {pwd_status}\n"
                f"域名: {len(self.config.domain_list)}个"
            )
            yield event.plain_result(info_msg)
        
        elif action == "proxy" and len(args) >= 3:
            proxy = args[2]
            self.config.proxy = proxy
            if self._update_astrbot_config("proxy", proxy):
                self.client_factory.update_option()
                yield event.plain_result(f"代理已设为: {proxy}")
        
        elif action == "noproxy":
            self.config.proxy = None
            if self._update_astrbot_config("proxy", ""):
                self.client_factory.update_option()
                yield event.plain_result("代理已清除")
                
        elif action == "threads" and len(args) >= 3:
            try:
                t = int(args[2])
                self.config.max_threads = t
                if self._update_astrbot_config("max_threads", t):
                    self.client_factory.update_option()
                    yield event.plain_result(f"线程数设为: {t}")
            except: pass

    def _update_astrbot_config(self, key: str, value) -> bool:
        try:
            config_dir = os.path.join(self.context.get_config().get("data_dir", "data"), "config")
            os.makedirs(config_dir, exist_ok=True)
            config_path = os.path.join(config_dir, f"astrbot_plugin_{self.plugin_name}_config.json")
            
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    config = json.load(f)
            else:
                config = {}
            
            config[key] = value
            
            with open(config_path, "w", encoding="utf-8-sig") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False

    # 保留其他实用指令
    @filter.command("jminfo")
    async def get_comic_info(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2: return
        comic_id = args[1]
        client = self.client_factory.create_client()
        try:
            album = client.get_album_detail(comic_id)
            cover_path = self.resource_manager.get_cover_path(comic_id)
            if not os.path.exists(cover_path):
                await self.downloader.download_cover(comic_id)
            yield event.chain_result(await self._build_album_message(client, album, comic_id, cover_path))
        except Exception as e:
            yield event.plain_result(f"获取信息失败: {str(e)}")

    @filter.command("jmsearch")
    async def search_comic(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 2: 
            yield event.plain_result("/jmsearch [关键词]")
            return
        keyword = parts[1]
        client = self.client_factory.create_client()
        try:
            search_res = client.search_site(keyword)
            msg = f"🔍 搜索: {keyword}\n"
            for i, (aid, title) in enumerate(search_res.iter_id_title()):
                if i >= 10: break
                msg += f"{aid} - {title}\n"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"搜索失败: {str(e)}")

    @filter.command("jmupdate")
    async def check_update(self, event: AstrMessageEvent):
        yield event.plain_result("JM-Cosmos v1.2.0: 已启用强制7z加密压缩与自动清理模式。")

    @filter.command("jmcleanup")
    async def cleanup_storage(self, event: AstrMessageEvent):
        count = self.resource_manager.cleanup_old_files()
        yield event.plain_result(f"已清理 {count} 个过期文件")
    
    @filter.command("jmdomain")
    async def manage_domain(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2: 
             yield event.plain_result("/jmdomain list | update")
             return
        op = args[1]
        if op == "list":
            yield event.plain_result(str(self.config.domain_list))
        elif op == "update":
            yield event.plain_result("正在更新域名，请稍候...")
            # 简化的更新逻辑，实际建议保留原版完整的crawler
            try:
                from curl_cffi import requests
                r = requests.get("https://jmcmomic.github.io/go/300.html", allow_redirects=False)
                # 简单解析示例
                new_domains = []
                for d in jmcomic.JmcomicText.analyse_jm_pub_html(r.text):
                     if "jm365" not in d: new_domains.append(d)
                
                if new_domains:
                    self.config.domain_list = new_domains[:3]
                    self._update_astrbot_config("domain_list", self.config.domain_list)
                    self.client_factory.update_option()
                    yield event.plain_result(f"已更新域名: {self.config.domain_list}")
                else:
                    yield event.plain_result("未获取到新域名")
            except Exception as e:
                yield event.plain_result(f"更新失败: {e}")
