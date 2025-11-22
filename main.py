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
from datetime import datetime
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
# 资源管理器
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

    def find_comic_folder(self, comic_id: str) -> Optional[str]:
        """
        查找漫画文件夹。
        策略1: 直接查找 ID 命名的文件夹 (配置强制规则后应命中此项)
        策略2: 遍历目录查找包含 ID 的文件夹 (兜底)
        """
        # 1. 尝试标准路径: downloads/12345
        target_path = os.path.join(self.downloads_dir, str(comic_id))
        if os.path.exists(target_path) and os.path.isdir(target_path):
            return target_path

        # 2. 兜底查找: 扫描 downloads 下所有文件夹
        # 防止配置未生效导致文件夹名为 "12345 标题" 或 "[12345]标题"
        try:
            if os.path.exists(self.downloads_dir):
                for name in os.listdir(self.downloads_dir):
                    full_path = os.path.join(self.downloads_dir, name)
                    if os.path.isdir(full_path):
                        # 检查文件夹名是否包含ID
                        if str(comic_id) in name:
                            return full_path
        except Exception as e:
            logger.error(f"查找文件夹出错: {e}")
            
        return None

    def cleanup_old_files(self, days=30):
        """简单清理过期文件"""
        cutoff = time.time() - (days * 86400)
        for folder in [self.archives_dir, self.covers_dir]:
            if not os.path.exists(folder): continue
            for f in os.listdir(folder):
                fp = os.path.join(folder, f)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
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
                "retry_times": 5,
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
                "rule": "Bd_Id"  # <--- 关键修复：强制文件夹名为 ID，避免使用标题
            },
            "plugins": {} # 禁用所有插件（包括img2pdf）
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
            # 在线程池中运行下载
            await loop.run_in_executor(self.executor, self._do_download, comic_id)
            return True, "下载完成"
        except Exception as e:
            logger.error(f"下载失败 {comic_id}: {e}")
            return False, str(e)
        finally:
            with self.lock:
                self.downloading.discard(comic_id)

    def _do_download(self, comic_id: str):
        # 使用配置好的 option 下载
        # 这里的 option 包含了 dir_rule: Bd_Id，所以会下载到 downloads/12345
        jmcomic.download_album(comic_id, self.factory.option)

# ===========================
# 插件主类
# ===========================

@register("jm_cosmos", "GEMILUXVII", "JM漫画下载(7z加密版)", "1.3.0")
class JMCosmosPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.plugin_name = "jm_cosmos"
        self.rm = ResourceManager(self.plugin_name)
        
        # 加载配置
        cfg_data = config or {}
        # 尝试读取本地配置如果传入为空
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
        is_custom = bool(pwd)
        if not pwd:
            pwd = f"jm{comic_id}"

        # 1. 检查是否已有压缩包
        if os.path.exists(archive_path):
            yield event.plain_result(f"检测到现有压缩包，正在发送...\n🔑 解压密码: {pwd}")
            yield event.chain_result([File(name=f"{comic_id}.7z", file=archive_path)])
            return

        yield event.plain_result(f"开始下载 {comic_id} ...")

        # 2. 执行下载
        success, msg = await self.downloader.download_comic(comic_id)
        if not success:
            yield event.plain_result(f"下载失败: {msg}")
            return

        # 3. 查找下载的文件夹
        comic_folder = self.rm.find_comic_folder(comic_id)
        if not comic_folder:
            yield event.plain_result("❌ 下载看似成功，但未找到文件夹。\n原因可能是文件名包含特殊字符或配置未生效。")
            return

        # 4. 压缩并删除
        try:
            yield event.plain_result("下载完成，正在进行极限压缩(7z)与加密...")
            
            await asyncio.to_thread(
                compress_folder_to_7z,
                input_folder=comic_folder,
                output_7z=archive_path,
                password=pwd,
                arcname=comic_id
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
            yield event.plain_result("/jmconfig password [密码] | proxy [url] | noproxy")
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
            # 保存到文件
            cfg_path = os.path.join(self.context.get_config().get("data_dir", "data"), "config", f"astrbot_plugin_{self.plugin_name}_config.json")
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8-sig") as f:
                # 构建要保存的字典，映射回 config 的字段
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
