from astrbot.api.message_components import *
from astrbot.api import logger
import astrbot.core.message.components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Video
from astrbot.core import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.all import *
import importlib
import subprocess
from PIL import Image as PILImage
import sys
import io
import random
import re
from datetime import datetime
from pathlib import Path # 确保 Path 已导入
import time
import aiohttp
import asyncio
from typing import Dict, Optional, List
from astrbot.core.utils.io import download_image_by_url
import base64
import uuid
import json
import os

@register("astrbot_plugin_sora", "CCYellowStar2", "使用API生成视频。指令: 文生视频 <提示词> 或 图生视频 <提示词> + 图片", "1.0")
class ApiVideoPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.api_key = config.get("api_key", "")
        self.api_url = config.get("api_url", "https://apis.kuai.host/v1/chat/completions")
        self.model = config.get("model", "sora-2-hd")
        self.session = aiohttp.ClientSession()
        
    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @filter.regex(r"^(文生视频)", priority=3)
    async def handle_text_to_video(self, event: AstrMessageEvent):
        user_prompt = re.sub(
            r"^(文生视频)\s*", "", event.message_obj.message_str, count=1
        ).strip()

        if not self.api_key:
            yield event.plain_result("错误：请先在配置文件中设置api_key")
            return
        
        if not user_prompt:
            yield event.plain_result("请输入视频生成的提示词。用法: 文生视频 <提示词>")
            return

        yield event.plain_result("收到文生视频请求，生成中请稍候...")
        content_list = [{"type": "text", "text": user_prompt}]
        payload = self._build_payload(content_list)
        
        await self._generate_and_send_video(event, payload)

    @filter.regex(r"^(图生视频)", priority=3)
    async def handle_image_to_video(self, event: AstrMessageEvent):
        user_prompt = re.sub(
            r"^(图生视频)\s*", "", event.message_obj.message_str, count=1
        ).strip()
        
        if not self.api_key:
            yield event.plain_result("错误：请先在配置文件中设置api_key")
            return
            
        image_list = await self.get_images(event)
        if not image_list:
            # 修改点 2: 更新帮助文本
            yield event.plain_result("请提供一张或多张图片来生成视频。用法: 图生视频 <提示词> + 图片")
            return

        if not user_prompt:
            user_prompt = "让画面动起来"
        
        # 修改点 3: 改进用户反馈，告知收到了多少张图片
        yield event.plain_result(f"收到图生视频请求，共计 {len(image_list)} 张图片，生成中请稍候...")
        
        # 修改点 4: 构建包含所有图片的 content_list
        # 首先添加文本部分
        content_list = [{"type": "text", "text": user_prompt}]
        
        # 然后遍历所有图片，为每一张图片创建一个 image_url 对象并添加
        for image_bytes in image_list:
            base64_image = base64.b64encode(image_bytes).decode("utf-8")
            image_payload_part = {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{base64_image}"}
            }
            content_list.append(image_payload_part)
        
        payload = self._build_payload(content_list)
        await self._generate_and_send_video(event, payload)

    def _build_payload(self, content_list: List[dict]) -> dict:
        return {
           "model": self.model,
           "max_tokens": 1000,
           "messages": [
              {
                 "role": "user",
                 "content": content_list
              }
           ]
        }

    async def _generate_and_send_video(self, event: AstrMessageEvent, payload: dict):
        headers = {'Accept': 'application/json', 'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        try:
            async with self.session.post(self.api_url, headers=headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    
                    # 仍然使用正则提取视频链接
                    match = re.search(r'\(([^)]+\.mp4)\)', content)
                    if match:
                        video_url = match.group(1)
                        logger.info(f"成功提取视频链接: {video_url}，尝试直接使用URL发送...")
                        
                        # 直接调用 Video.fromURL 发送
                        await event.send(event.chain_result([Video.fromURL(url=video_url)]))
                        logger.info("已成功向框架提交视频URL。")
                        
                    else:
                        error_msg = f"未能从API响应中提取视频链接。响应内容: {content}"
                        logger.error(error_msg)
                        await self.context.send_message(event.unified_msg_origin, MessageChain().message(error_msg))
                else:
                    error_text = await response.text()
                    error_msg = f"API请求失败，状态码: {response.status}, 响应: {error_text}"
                    logger.error(error_msg)
                    await self.context.send_message(event.unified_msg_origin, MessageChain().message(error_msg))

        except Exception as e:
            logger.error(f"视频生成过程中发生严重错误: {e}", exc_info=True)
            await self.context.send_message(event.unified_msg_origin, MessageChain().message(f"生成失败: {str(e)}"))

        
    async def get_images(self, event: AstrMessageEvent) -> list[bytes]:
        images = []
        for s in event.message_obj.message:
            if isinstance(s, Comp.Reply) and s.chain:
                for seg in s.chain:
                    if isinstance(seg, Comp.Image):
                        if seg.url and (img := await self._load_bytes(seg.url)):
                            images.append(img)
                        elif seg.file and (img := await self._load_bytes(seg.file)):
                            images.append(img)
        if images: return images
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Image):
                if seg.url and (img := await self._load_bytes(seg.url)):
                    images.append(img)
                elif seg.file and (img := await self._load_bytes(seg.file)):
                    images.append(img)
        if images: return images
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.At):
                if avatar := await self._get_avatar(str(seg.qq)):
                    images.append(avatar)
        return images

    async def _download_image(self, url: str) -> bytes | None:
        try:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as e:
            logger.error(f"图片下载失败: {e}")
            return None

    async def _get_avatar(self, user_id: str) -> bytes | None:
        if not user_id.isdigit(): user_id = "".join(random.choices("0123456789", k=9))
        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        try:
            async with self.session.get(avatar_url, timeout=10) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as e:
            logger.error(f"下载头像失败: {e}")
            return None

    def _extract_first_frame_sync(self, raw: bytes) -> bytes:
        img_io = io.BytesIO(raw)
        img = PILImage.open(img_io)
        if img.format != "GIF": return raw
        logger.info("检测到GIF, 将抽取 GIF 的第一帧来生图")
        first_frame = img.convert("RGBA")
        out_io = io.BytesIO()
        first_frame.save(out_io, format="PNG")
        return out_io.getvalue()

    async def _load_bytes(self, src: str) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()
        path = Path(src)
        if path.is_file():
            raw = await loop.run_in_executor(None, path.read_bytes)
        elif src.startswith("http"):
            raw = await self._download_image(src)
        elif src.startswith("base64://"):
            raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
        if not raw: return None
        return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)
