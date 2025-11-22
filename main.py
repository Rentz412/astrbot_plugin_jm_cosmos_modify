from astrbot.api.message_components import Image, Plain, File
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

import asyncio
import os
import yaml
import re
import json
import py7zr
import shutil
import time
import concurrent.futures
from typing import Dict, List, Set, Tuple, Optional, Any
from dataclasses import dataclass
from threading import Lock

import jmcomic

# ===========================
# 辅助函数
# ===========================

def validate_comic_id(comic_id: str) -> bool:
    """验证漫画ID格式，防止路径遍历"""
    if not re.match(r"^\d+$", comic_id):
        return False
    if len(comic_id) > 20:
        return False
    return True

# ===========================
# 配置类
# ===========================

@dataclass
class CosmosConfig:
    """Cosmos插件配置类"""
    domain_list: List[str]
    proxy: Optional[str]
    avs_cookie: str
    max_threads: int
    debug_mode: bool
    show_cover: bool
    custom_password: str

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "CosmosConfig":
        return cls(
            domain_list=config_dict.get("domain_list", ["18comic.vip", "jm365.xyz", "18comic.org"]),
            proxy=config_dict.get("proxy"),
            avs_cookie=config_dict.get("avs_cookie", ""),
            max_threads=config_dict.get("max_threads", 10),
            debug_mode=config_dict.get("debug_mode", False),
            show_cover=config_dict.get("show_cover", True),
            custom_password=config_dict.get("custom_password", "")
        )

# ===========================
# 资源管理器 (已还原原始逻辑)
# ===========================

class ResourceManager:
    """管理文件路径和目录"""

    def __init__(self, plugin_name: str):
        self.base_dir = StarTools.get_data_dir(plugin_name)
        self.downloads_dir = os.path.join(self.base_dir, "downloads")
        self.archives_dir = os.path.join(self.base_dir, "archives")
        self.logs_dir = os.path.join(self.base_dir, "logs")
        self.covers_dir = os.path.join(self.base_dir, "covers")
        
        # 确保目录存在
        for d in [self.downloads_dir, self.archives_dir, self.logs_dir, self.covers_dir]:
            os.makedirs(d, exist_ok=True)

    def get_archive_path(self, comic_id: str) -> str:
        return os.path.join(self.archives_dir, f"{comic_id}.7z")

    def get_cover_path(self, comic_id: str) -> str:
        return os.path.join(self.covers_dir, f"{comic_id}.jpg")

    def find_comic_folder(self, comic_id: str) -> str:
        """
        【已还原】原始 main.py 中的智能查找逻辑
        查找漫画文件夹，支持多种命名方式
        """
        logger.info(f"开始查找漫画ID {comic_id} 的文件夹")

        # 1. 尝试直接匹配ID
        id_path = os.path.join(self.downloads_dir, str(comic_id))
        if os.path.exists(id_path):
            logger.info(f"找到直接匹配的目录: {id_path}")
            return id_path

        # 2. 尝试查找以漫画标题命名的目录
        if os.path.exists(self.downloads_dir):
            exact_matches = []
            partial_matches = []

            try:
                for item in os.listdir(self.downloads_dir):
                    item_path = os.path.join(self.downloads_dir, item)
                    if not os.path.isdir(item_path):
                        continue

                    # 精确匹配逻辑：目录名以ID开头或结尾，或者格式为 [ID]...
                    if (
                        item.startswith(str(comic_id) + "_")
                        or item.endswith("_" + str(comic_id))
                        or item.startswith("[" + str(comic_id) + "]")
                        or item == str(comic_id)
                    ):
                        exact_matches.append(item_path)
                        logger.info(f"找到精确匹配的漫画目录: {item_path}")
                    # 部分匹配逻辑：目录名包含ID且是独立数字
                    elif str(comic_id) in item:
                        pattern = r"\b" + re.escape(str(comic_id)) + r"\b"
                        if re.search(pattern, item):
                            partial_matches.append(item_path)
                            logger.info(f"找到部分匹配的漫画目录: {item_path}")
            except Exception as e:
                logger.error(f"遍历目录出错: {e}")

            # 优先返回精确匹配
            if exact_matches:
                logger.info(f"找到精确匹配，返回: {exact_matches[0]}")
                return exact_matches[0]
            elif partial_matches:
                logger.info(f"找到部分匹配，返回: {partial_matches[0]}")
                return partial_matches[0]

        # 默认返回downloads目录下的ID路径 (即使不存在)
        default_path = os.path.join(self.downloads_dir, str(comic_id))
        logger.info(f"未找到现有目录，返回默认路径: {default_path}")
        return default_path

    def cleanup_old_files(self, days=30):
        cutoff = time.time() - (days * 86400)
        for folder in [self.archives_dir, self.covers_dir]:
            if not os.path.exists(folder): continue
            for f in os.listdir(folder):
                fp = os.path.join(folder, f)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                except: pass

    def clear_cover_cache(self):
        if os.path.exists(self.covers_dir):
            try:
                for f in os.listdir(self.covers_dir):
                    os.remove(os.path.join(self.covers_dir, f))
            except: pass

# ===========================
# 压缩工具
# ===========================

def compress_folder_to_7z(input_folder: str, output_7z: str, password: str, arcname: str):
    """7z 极限压缩并加密"""
    with py7zr.SevenZipFile(
        output_7z,
        mode="w",
        password=password,
        filters=[{"id": py7zr.FILTER_LZMA2, "preset": 9}] 
    ) as archive:
        # 将整个文件夹写入压缩包
        archive.writeall(input_folder, arcname=arcname)

# ===========================
# JM 客户端工厂
# ===========================

class JMClientFactory:
    def __init__(self, config: CosmosConfig, resource_manager: ResourceManager):
        self.config = config
        self.rm = resource_manager
        self.option = self._create_option()

    def _create_option(self):
        option_dict = {
            "client": {
                "domain": self.config.domain_list,
                "retry_times": 3,
                "postman": {
                    "meta_data": {
                        "proxies": {"https": self.config.proxy} if self.config.proxy else None,
                        "cookies": {"AVS": self.config.avs_cookie},
                        "headers": {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
                        }
                    }
                }
            },
            "download": {
                "thread_count": self.config.max_threads,
                "cache": True,
                "image": {"decode": True, "suffix": ".jpg"},
            },
            "dir_rule": {
                "base_dir": self.rm.downloads_dir,
                # 【已还原】移除 Bd_Id 规则，恢复 jmcomic 默认行为（使用标题命名）
            },
            "plugins": {} 
        }
        return jmcomic.create_option_by_str(yaml.safe_dump(option_dict))

    def update_option(self):
        self.option = self._create_option()

# ===========================
# 下载器
# ===========================

class ComicDownloader:
    def __init__(self, factory: JMClientFactory, config: CosmosConfig):
        self.factory = factory
        self.config = config
        self.downloading = set()
        self.lock = Lock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    async def download_comic(self, comic_id: str) -> Tuple[bool, str]:
        with self.lock:
            if comic_id in self.downloading:
                return False, "正在下载中"
            self.downloading.add(comic_id)

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self.executor, self._do_download, comic_id)
            return True, "下载完成"
        except Exception as e:
            # 这里的异常通常是网络连不通，和文件夹无关
            logger.error(f"下载失败 {comic_id}: {e}")
            return False, str(e)
        finally:
            with self.lock:
                self.downloading.discard(comic_id)

    def _do_download(self, comic_id: str):
        jmcomic.download_album(comic_id, self.factory.option)

# ===========================
# 插件主类
# ===========================

@register("jm_cosmos", "GEMILUXVII", "JM漫画下载(7z加密版)", "1.4.0")
class JMCosmosPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.plugin_name = "jm_cosmos"
        self.rm = ResourceManager(self.plugin_name)
        self.rm.clear_cover_cache()
        
        # 加载配置
        cfg_data = config or {}
        if not config:
            cfg_path = os.path.join(context.get_config().get("data_dir", "data"), "config", f"astrbot_plugin_{self.plugin_name}_config.json")
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8-sig") as f:
                        cfg_data = json.load(f)
                except: pass

        self.config = CosmosConfig.from_dict(cfg_data)
        self.factory = JMClientFactory(self.config, self.rm)
        self.downloader = ComicDownloader(self.factory, self.config)

    @filter.command("jm")
    async def cmd_jm(self, event: AstrMessageEvent):
        """下载漫画并打包为7z
        用法: /jm [ID]
        """
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("请输入漫画ID")
            return
        
        comic_id = args[1]
        if not validate_comic_id(comic_id):
            yield event.plain_result("ID格式错误")
            return

        archive_path = self.rm.get_archive_path(comic_id)
        
        # 确定密码
        pwd = self.config.custom_password.strip()
        if not pwd:
            pwd = f"jm{comic_id}"

        # 1. 检查是否已有压缩包
        if os.path.exists(archive_path):
            yield event.plain_result(f"检测到现有压缩包，直接发送...\n🔑 解压密码: {pwd}")
            yield event.chain_result([File(name=f"{comic_id}.7z", file=archive_path)])
            return

        yield event.plain_result(f"开始下载 {comic_id} (文件夹名称将包含标题)...")

        # 2. 执行下载
        # 注意：如果网络不通，这里依然会报错 "请求重试全部失败"
        success, msg = await self.downloader.download_comic(comic_id)
        if not success:
            yield event.plain_result(f"❌ 下载失败: {msg}\n(请检查域名或代理配置)")
            return

        # 3. 使用【还原的智能逻辑】查找下载文件夹
        comic_folder = self.rm.find_comic_folder(comic_id)
        
        # 再次确认文件夹是否存在 (find_comic_folder 兜底会返回不存在的默认路径)
        if not os.path.exists(comic_folder) or not os.path.isdir(comic_folder):
            # 如果智能查找都找不到，说明下载真的没成功保存
            yield event.plain_result(f"❌ 下载流程结束但未找到漫画文件夹。\n(可能原因：网络下载中断 或 目录权限不足)")
            return

        # 4. 压缩并删除
        try:
            yield event.plain_result(f"✅ 已定位文件夹: {os.path.basename(comic_folder)}\n正在进行7z极限压缩与加密...")
            
            await asyncio.to_thread(
                compress_folder_to_7z,
                input_folder=comic_folder,
                output_7z=archive_path,
                password=pwd,
                arcname=os.path.basename(comic_folder) # 压缩包内保留原始文件夹名
            )

            # 删除原图目录
            shutil.rmtree(comic_folder)
            
            yield event.plain_result(f"✅ 打包完成！\n🔑 解压密码: {pwd}")
            yield event.chain_result([File(name=f"{comic_id}.7z", file=archive_path)])

        except Exception as e:
            logger.error(f"打包失败: {e}")
            yield event.plain_result(f"打包过程出错: {e}")

    @filter.command("jmconfig")
    async def cmd_config(self, event: AstrMessageEvent):
        """配置管理"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /jmconfig password [密码] | proxy [url] | noproxy")
            return
        
        op = args[1]
        save_needed = False
        
        if op == "password":
            self.config.custom_password = " ".join(args[2:]) if len(args) > 2 else ""
            save_needed = True
            yield event.plain_result(f"自定义密码已{'设置' if self.config.custom_password else '清除'}")
            
        elif op == "proxy":
            if len(args) > 2:
                self.config.proxy = args[2]
                save_needed = True
                self.factory.update_option()
                yield event.plain_result(f"代理已设为 {self.config.proxy}")
                
        elif op == "noproxy":
            self.config.proxy = None
            save_needed = True
            self.factory.update_option()
            yield event.plain_result("代理已清除")

        if save_needed:
            cfg_path = os.path.join(self.context.get_config().get("data_dir", "data"), "config", f"astrbot_plugin_{self.plugin_name}_config.json")
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8-sig") as f:
                d = {
                    "domain_list": self.config.domain_list,
                    "proxy": self.config.proxy,
                    "avs_cookie": self.config.avs_cookie,
                    "max_threads": self.config.max_threads,
                    "custom_password": self.config.custom_password
                }
                json.dump(d, f, indent=2, ensure_ascii=False)
    
    @filter.command("jmdomain")
    async def cmd_domain(self, event: AstrMessageEvent):
        """更新域名"""
        yield event.plain_result("正在获取最新域名...")
        try:
            from curl_cffi import requests
            resp = await asyncio.to_thread(requests.get, "https://jmcmomic.github.io/go/300.html", allow_redirects=False)
            new_domains = []
            for d in jmcomic.JmcomicText.analyse_jm_pub_html(resp.text):
                if "jm365" not in d: new_domains.append(d)
            
            if new_domains:
                self.config.domain_list = new_domains[:3]
                self.factory.update_option()
                yield event.plain_result(f"域名已更新: {self.config.domain_list}")
            else:
                yield event.plain_result("未找到可用域名")
        except Exception as e:
            yield event.plain_result(f"更新失败: {e}")
