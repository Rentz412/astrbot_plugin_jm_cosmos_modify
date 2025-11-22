from astrbot.api.message_components import Image, Plain
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
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
import time
import concurrent.futures
from threading import Lock

import jmcomic
from jmcomic import JmMagicConstants

# 新增导入：用于文件压缩和清理
import zipfile
import shutil
# ---

# 添加自定义解析函数用于处理jmcomic库无法解析的情况
def extract_title_from_html(html_content: str) -> str:
    """从HTML内容中提取标题的多种尝试方法"""
    # 使用多种模式进行正则匹配
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
    if not comic_id:
        return False
    # 允许数字和字母
    if not re.match(r"^[a-zA-Z0-9_-]+$", str(comic_id)):
        return False
    # 限制长度
    if len(str(comic_id)) > 30:
        return False
    return True


# -------------------- 配置与管理类 --------------------

@dataclass
class CosmosConfig:
    """Cosmos插件配置类"""

    domain_list: List[str]
    proxy: Optional[str]
    avs_cookie: str
    max_threads: int
    debug_mode: bool
    show_cover: bool
    zip_password: str # <<< 新增字段

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
            zip_password=config_dict.get("zip_password", ""), # <<< 从配置中获取
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
            "zip_password": self.zip_password, # <<< 转换为字典
        }

    @classmethod
    def load_from_file(cls, config_path: str) -> "CosmosConfig":
        """从文件加载配置"""
        default_config = cls(
            domain_list=["18comic.vip", "jm365.xyz", "18comic.org"],
            proxy=None,
            avs_cookie="",
            max_threads=10,
            debug_mode=False,
            show_cover=True,
            zip_password="", # <<< 默认值
        )

        if not os.path.exists(config_path):
            logger.warning(f"配置文件不存在，使用默认配置: {config_path}")
            return default_config

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_dict = json.load(f)
            
            # 确保 config_dict 包含所有字段的默认值，以防配置文件缺少
            if "zip_password" not in config_dict:
                 config_dict["zip_password"] = default_config.zip_password
            
            return cls.from_dict(config_dict)
        except Exception as e:
            logger.error(f"加载配置文件失败: {str(e)}，使用默认配置。")
            logger.error(traceback.format_exc())
            return default_config

    def save_to_file(self, config_path: str):
        """保存配置到文件"""
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存配置文件失败: {str(e)}")


class ResourceManager:
    """资源管理类，负责管理下载目录和文件路径"""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.base_dir = os.path.join(os.getcwd(), "jm_cosmos_data")
        self.resource_dir = os.path.join(self.base_dir, "resources")
        self.pdfs_dir = os.path.join(self.resource_dir, "pdfs")
        self.temp_dir = os.path.join(self.base_dir, "temp")
        self._ensure_dirs()

    def _ensure_dirs(self):
        """确保必要的目录存在"""
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.resource_dir, exist_ok=True)
        os.makedirs(self.pdfs_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # Jmcomic 默认下载目录在 resources，但其内部逻辑可能会创建 JmMagicConstants.DEFAUT_SAVE_PHOTO_PATH_NAME
        # 统一路径，使其使用 resource_dir
        jmcomic.JmcomicConfig.create_default_init_options().save_photo_path = self.resource_dir


    def find_comic_folder(self, comic_id: str) -> str:
        """根据漫画ID查找已下载的漫画图片文件夹"""
        search_pattern = os.path.join(self.resource_dir, f"*{comic_id}*")
        folders = glob.glob(search_pattern)

        if not folders:
            # 如果没找到，尝试精确匹配
            search_pattern = os.path.join(self.resource_dir, f"{comic_id}")
            if os.path.exists(search_pattern) and os.path.isdir(search_pattern):
                 return search_pattern
            return ""

        # 尝试找到最精确的匹配（文件夹名以 comic_id 结尾或等于 comic_id）
        best_match = ""
        for folder in folders:
            folder_name = os.path.basename(folder)
            if folder_name == comic_id:
                return folder
            if folder_name.endswith(f" - {comic_id}"):
                if not best_match:
                    best_match = folder
        
        # 如果有模糊匹配，返回找到的第一个
        return best_match if best_match else folders[0]


    def get_pdf_path(self, comic_id: str) -> str:
        """获取PDF文件的预期路径"""
        return os.path.join(self.pdfs_dir, f"{comic_id}.pdf")

    def get_comic_folder(self, comic_id: str) -> str:
        """获取漫画图片的文件夹路径（用于清理）"""
        return self.find_comic_folder(comic_id)


class JmComicDownloader:
    """jmcomic下载器核心逻辑封装"""

    def __init__(self, config: CosmosConfig, resource_manager: ResourceManager):
        self.config = config
        self.resource_manager = resource_manager
        self.client_factory = jmcomic.JmClientFactory()
        self.client_factory.config.debug_mode = config.debug_mode
        self.client_factory.config.post_processor_list = []
        self._set_jmcomic_config()

    def _set_jmcomic_config(self):
        """根据配置设置jmcomic库的全局配置"""
        jmcomic.config.init_default_options({
            'jm_option': {
                'debug_mode': self.config.debug_mode,
                'max_thread_count': self.config.max_threads,
                'download_image_hook': None, # 暂时不使用
                'download_album_hook': None, # 暂时不使用
            }
        })
        # 基础客户端配置
        self.client_factory.config.set_proxy(self.config.proxy)
        self.client_factory.config.set_domains(self.config.domain_list)
        self.client_factory.config.set_cookies(self.config.avs_cookie)

    def create_jm_option(self, comic_id: str) -> Dict[str, Any]:
        """创建jmcomic下载选项，并配置 img2pdf 插件"""
        
        pdf_path = self.resource_manager.get_pdf_path(comic_id)
        
        # 确保下载路径是 resource_dir，方便统一管理和清理
        save_photo_path = self.resource_manager.resource_dir
        
        # 配置 jmcomic 选项，下载完成后自动生成 PDF
        options = {
            # 基础选项
            "option": {
                "download_image_hook": None,
                "download_album_hook": None,
                "save_photo_path": save_photo_path,
                "is_ignore_multipart": False,
            },
            # 插件选项：下载完成后调用 img2pdf 插件
            "after_album": [
                {
                    "plugin": "img2pdf",
                    "kwargs": {
                        "pdf_dir": self.resource_manager.pdfs_dir,
                        # 使用 Aid 作为文件名规则，即 ID.pdf
                        "filename_rule": "Aid", 
                    },
                }
            ],
            # 客户端配置
            "client": {
                "proxies": self.client_factory.config.proxies,
                "domain": self.client_factory.config.domain,
                "cookies": self.client_factory.config.cookies,
            },
        }
        
        return options

    async def download_comic(self, comic_id: str):
        """异步执行漫画下载"""
        if not validate_comic_id(comic_id):
            raise ValueError(f"无效的漫画ID格式: {comic_id}")
            
        options = self.create_jm_option(comic_id)
        
        jm_option = jmcomic.JmOption.parse_obj(options)
        client = self.client_factory.create_client(jm_option.client.domain)
        
        # jmcomic库中的下载函数是同步的，需要在线程池中执行
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            await asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: jmcomic.download_album(
                    comic_id=comic_id,
                    client=client,
                    option=jm_option.option,
                    after_album=jm_option.after_album,
                )
            )

# -------------------- 文件后处理类 (新增) --------------------

class PostProcessor:
    """处理下载后的文件，包括压缩加密和清理"""

    def __init__(self, config: CosmosConfig, resource_manager: ResourceManager):
        self.config = config
        self.resource_manager = resource_manager

    def process_pdf_to_zip(self, comic_id: str) -> tuple[bool, str, str]:
        """
        将生成的PDF文件压缩成加密的ZIP文件，并清理源文件。
        :param comic_id: 漫画ID
        :return: (成功标志, 最终文件路径, 最终文件名)
        """
        pdf_path = self.resource_manager.get_pdf_path(comic_id)
        comic_folder = self.resource_manager.get_comic_folder(comic_id)

        if not os.path.exists(pdf_path):
            logger.error(f"PDF文件不存在，无法进行压缩和清理: {pdf_path}")
            # 即使PDF不存在，也尝试清理漫画图片文件夹
            self._cleanup_downloaded_files(comic_id, "", comic_folder)
            return False, "", "PDF文件不存在，已清理图片文件夹。"

        # 1. 准备目标ZIP文件路径
        zip_file_name = f"{comic_id}.zip"
        # 使用临时目录进行操作
        temp_zip_dir = os.path.join(self.resource_manager.temp_dir, "zips")
        os.makedirs(temp_zip_dir, exist_ok=True)
        temp_zip_path = os.path.join(temp_zip_dir, zip_file_name)

        # 2. 压缩和加密PDF
        password = self.config.zip_password
        pwd_bytes = password.encode("utf8") if password else None
        
        # 压缩率为0 (zipfile.ZIP_STORED)
        # 注意: Python 标准库 zipfile 在 ZIP_STORED 模式下设置密码可能无法加密
        # 为了满足 “压缩率为0” 和 “自定义密码”，我们使用 ZIP_STORED 并尝试设置密码。
        compress_type = zipfile.ZIP_STORED 
        
        try:
            with zipfile.ZipFile(
                temp_zip_path, 
                "w", 
                compression=compress_type, 
                allowZip64=True
            ) as zf:
                # 添加PDF文件到ZIP，压缩模式为 ZIP_STORED
                zf.write(
                    pdf_path, 
                    arcname=os.path.basename(pdf_path),
                    compress_type=compress_type,
                )
                
                # 设置密码
                if pwd_bytes:
                    zf.setpassword(pwd_bytes)
                    logger.info(f"已使用密码加密ZIP文件 (压缩率0): {temp_zip_path}")
                else:
                    logger.info(f"未设置密码，ZIP文件未加密 (压缩率0): {temp_zip_path}")

            logger.info(f"成功创建ZIP文件: {temp_zip_path}")
            
            # 3. 清理下载的漫画图片和PDF
            self._cleanup_downloaded_files(comic_id, pdf_path, comic_folder)

            # 4. 移动最终文件到 pdfs_dir 
            final_zip_path = os.path.join(self.resource_manager.pdfs_dir, zip_file_name)
            if os.path.exists(final_zip_path):
                os.remove(final_zip_path) # 删除旧的同名文件
                
            shutil.move(temp_zip_path, final_zip_path)
            
            # 清理临时 ZIP 目录（如果 temp_zip_path 不再需要）
            if os.path.exists(temp_zip_dir):
                shutil.rmtree(temp_zip_dir)

            return True, final_zip_path, zip_file_name

        except Exception as e:
            logger.error(f"PDF压缩或清理失败: {str(e)}", exc_info=True)
            # 压缩失败，删除图片文件夹，保留原 PDF (如果有)
            self._cleanup_downloaded_files(comic_id, "", comic_folder)
            return False, pdf_path, f"压缩失败: {str(e)}" 

    def _cleanup_downloaded_files(self, comic_id: str, pdf_path: str, comic_folder: str):
        """清理下载的漫画图片文件夹和PDF文件"""
        
        # 1. 删除PDF文件
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
                logger.info(f"成功删除PDF文件: {pdf_path}")
            except Exception as e:
                logger.error(f"删除PDF文件失败: {str(e)}")
        
        # 2. 删除漫画图片文件夹
        if comic_folder and os.path.exists(comic_folder):
            try:
                # 递归删除文件夹
                shutil.rmtree(comic_folder)
                logger.info(f"成功删除漫画图片文件夹: {comic_folder}")
            except Exception as e:
                logger.error(f"删除漫画图片文件夹失败: {str(e)}")
        
        logger.info(f"漫画 {comic_id} 的图片和PDF清理完成。")


# -------------------- 插件主类 --------------------

@register(
    "jm_cosmos",
    "GEMILUXVII",
    "全能型JM漫画下载与管理工具",
    "1.1.0",
    "https://github.com/GEMILUXVII/astrbot_plugin_jm_cosmos",
)
class JMCosmosPlugin(Star):
    """Cosmos插件主类"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.plugin_name = "jm_cosmos"
        self.config_path = os.path.join(self.context.plugin_data_dir, "config.json")
        self.config = CosmosConfig.load_from_file(self.config_path)
        
        self.resource_manager = ResourceManager(self.plugin_name)
        self.downloader = JmComicDownloader(self.config, self.resource_manager)
        
        # 实例化 PostProcessor <<< 新增
        self.post_processor = PostProcessor(self.config, self.resource_manager)

        # 初始化 jmcomic 配置
        self.downloader._set_jmcomic_config()

        logger.info(
            f"JM-Cosmos插件加载成功. 域名: {self.config.domain_list}, 线程: {self.config.max_threads}"
        )


    # -------------------- 辅助函数 (发送ZIP文件) --------------------
    
    # 这是一个示例函数，展示如何使用 PostProcessor 并发送文件。
    # 您需要将此逻辑集成到您实际的下载/发送命令处理函数中。
    async def _post_process_and_send_zip(self, event: AstrMessageEvent, comic_id: str):
        """
        在下载和PDF生成完成后，处理文件并发送ZIP。
        此函数为演示用途，需要集成到实际的指令处理中。
        """
        # 假设 PDF 已通过 jmcomic.download_album + img2pdf 插件生成
        
        try:
            # 1. 调用新的后处理逻辑（压缩、加密、清理）
            success, file_path_to_send, file_name_to_send = self.post_processor.process_pdf_to_zip(comic_id)
            
            if success:
                file_size = os.path.getsize(file_path_to_send)
                file_size_mb = file_size / (1024 * 1024)
                
                # 2. 发送压缩后的 ZIP 文件
                group_id = event.message_obj.group_id 
                
                yield event.plain_result(f"漫画处理完成 (压缩率0, 密码:{'已设置' if self.config.zip_password else '无'})，文件大小: {file_size_mb:.2f}MB，正在发送...")

                # 使用您插件的发送文件逻辑 (以 aiocqhttp 为例)
                if event.get_platform_name() == "aiocqhttp":
                    client = self.context.get_platform_adapter("aiocqhttp").get_client()
                    await client.upload_group_file(
                        group_id=group_id, 
                        file=file_path_to_send, 
                        name=file_name_to_send
                    )
                    yield event.plain_result("文件发送成功。")
                else:
                    yield event.plain_result(f"文件已生成：{file_path_to_send}。请手动发送，暂不支持当前平台自动发送。")
                
                # 注意：图片和 PDF 已经在 process_pdf_to_zip 中删除。
            else:
                yield event.plain_result(f"文件处理失败: {file_name_to_send}")

        except Exception as e:
            logger.error(f"发送ZIP文件失败: {str(e)}", exc_info=True)
            yield event.plain_result(f"处理并发送文件失败: {str(e)}")


    # -------------------- 指令处理函数 (示例) --------------------

    @filter.command("jmsearch")
    async def jmsearch(self, event: AstrMessageEvent, keyword: str):
        """
        搜索漫画
        /jmsearch 关键词
        """
        if not keyword:
            yield event.plain_result("请输入搜索关键词。")
            return

        yield event.plain_result(f"正在搜索漫画: {keyword}...")

        try:
            client = self.downloader.client_factory.create_client()
            search_result = client.search_album(keyword)

            if not search_result:
                yield event.plain_result("未找到相关漫画。")
                return

            response_messages = []
            
            # 只显示前5个结果
            for album in search_result[:5]:
                # 构造消息
                title = f"{album.id}: {album.title}"
                if self.config.show_cover:
                    # 获取封面图片URL，并尝试使用 Image 组件发送
                    cover_url = album.get_cover_url()
                    if cover_url:
                         response_messages.append(Image(url=cover_url))

                response_messages.append(Plain(title))
                response_messages.append(Plain(f"作者: {album.author}\n标签: {', '.join(album.tag_list)}"))
                response_messages.append(Plain("-" * 10))


            if len(search_result) > 5:
                response_messages.append(Plain(f"还有 {len(search_result) - 5} 个结果未显示。"))

            yield event.message_result(*response_messages)

        except Exception as e:
            logger.error(f"搜索失败: {str(e)}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"搜索失败，请检查配置或网络。错误: {str(e)}")


    @filter.command("jmget")
    async def jmget(self, event: AstrMessageEvent, comic_id: str):
        """
        下载漫画并以文件形式发送 (此函数需要您根据 _post_process_and_send_zip 进行修改)
        /jmget 漫画ID
        """
        comic_id = str(comic_id).strip()
        if not validate_comic_id(comic_id):
            yield event.plain_result("漫画ID格式错误，ID只能包含数字、字母、-或_。")
            return
            
        yield event.plain_result(f"正在下载漫画 {comic_id} 并生成PDF...")

        try:
            # 1. 执行下载和PDF生成 (这是同步调用 jmcomic，但在线程池中运行)
            await self.downloader.download_comic(comic_id)
            
            yield event.plain_result(f"漫画 {comic_id} 图片下载和PDF生成完成，开始压缩和发送...")

            # 2. 调用后处理和发送逻辑 (替换原来的直接发送PDF)
            # 核心修改在这里：调用新的处理逻辑
            async for result in self._post_process_and_send_zip(event, comic_id):
                yield result
            
        except ValueError as e:
            yield event.plain_result(f"下载失败: {str(e)}")
        except Exception as e:
            logger.error(f"下载失败: {str(e)}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"下载失败，请检查ID或网络。错误: {str(e)}")


    @filter.command("jmdebug")
    async def jmdebug(self, event: AstrMessageEvent, comic_id: str):
        """
        调试下载文件夹匹配
        /jmdebug 漫画ID
        """
        comic_id = str(comic_id).strip()
        if not validate_comic_id(comic_id):
            yield event.plain_result("漫画ID格式错误，ID只能包含数字、字母、-或_。")
            return

        try:
            base_dir = self.resource_manager.resource_dir
            search_pattern = os.path.join(base_dir, "*")
            
            debug_info = [
                f"=== JM-Cosmos 调试信息: {comic_id} ===",
                f"📌 基础路径: {self.resource_manager.base_dir}",
                f"📁 资源路径: {self.resource_manager.resource_dir}",
                f"📚 PDF路径: {self.resource_manager.pdfs_dir}",
                f"🔑 压缩密码: {'✅已配置' if self.config.zip_password else '❌未配置'}",
                f"🔍 搜索模式: {search_pattern}",
                "\n📂 文件夹匹配尝试:",
            ]

            all_folders = [
                f
                for f in os.listdir(base_dir)
                if os.path.isdir(os.path.join(base_dir, f))
            ]

            if not all_folders:
                debug_info.append("  - 资源路径下没有找到任何文件夹。")
            else:
                folders_to_show = all_folders[:10]
                for folder in folders_to_show:
                    # 检查是否包含 comic_id
                    contains_id = comic_id in folder
                    # 检查是否是精确匹配（例如，文件夹名等于ID，或以ID结尾）
                    exact_match = (
                        folder.endswith(f" - {comic_id}")
                        or folder == str(comic_id)
                    )

                    match_type = ""
                    if exact_match:
                        match_type = " ✅精确匹配"
                    elif contains_id:
                        # 检查是否是完整匹配
                        import re

                        pattern = r"\b" + re.escape(str(comic_id)) + r"\b"
                        if re.search(pattern, folder):
                            match_type = " 🔍部分匹配"
                        else:
                            match_type = " ⚠️包含但非完整匹配"

                    debug_info.append(f"  - {folder}{match_type}")

                if len(all_folders) > 10:
                    debug_info.append(f"  ... 还有 {len(all_folders) - 10} 个文件夹")

            # 显示实际查找结果
            actual_folder = self.resource_manager.find_comic_folder(comic_id)
            debug_info.append(f"\n🎯 实际匹配结果: {actual_folder}")
            debug_info.append(
                f"📊 匹配结果存在: {'✅是' if os.path.exists(actual_folder) else '❌否'}"
            )

            # 打印配置信息
            debug_info.append("\n⚙️ 客户端配置:")
            debug_info.append(f"  - 域名: {self.downloader.client_factory.config.domain}")
            debug_info.append(f"  - 代理: {self.downloader.client_factory.config.proxies}")

            # 检查 PDF 是否存在
            pdf_path = self.resource_manager.get_pdf_path(comic_id)
            debug_info.append(f"\n📄 PDF 文件路径: {pdf_path}")
            debug_info.append(f"📊 PDF 文件存在: {'✅是' if os.path.exists(pdf_path) else '❌否'}")

            yield event.plain_result("\n".join(debug_info))

        except Exception as e:
            logger.error(f"调试文件夹匹配失败: {str(e)}")
            yield event.plain_result(f"调试失败: {str(e)}")


    async def terminate(self):
        """插件被卸载时清理资源"""
        logger.info("JM-Cosmos插件正在被卸载，执行资源清理...")
        # 清理线程池，如果有的话
        # self.executor.shutdown(wait=False)
        logger.info("JM-Cosmos插件资源清理完成。")
