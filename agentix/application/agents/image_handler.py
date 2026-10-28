"""
Image handling module - image recognition and generation.
"""

import asyncio
import base64
import io
from typing import Dict, List

import aiohttp
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger
from PIL import Image

from agentix.application.agents.constants import IMAGE_NOT_FOUND_MESSAGE
from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.application.agents.lg_prompts import (
    GET_IMAGE_SYSTEM_PROMPT,
    IMAGE_GENERATION_ENHANCE_PROMPT,
    IMAGE_GENERATION_SUCCESS_PROMPT,
)
from agentix.application.agents.llm_factory import get_llm_client
from agentix.config import settings
from agentix.domain.policies.upload_paths import resolve_within_root


async def _generate_image(user_query: str, state: AgentState) -> Dict[str, List[BaseMessage]]:
    """Generate image using CogView-4 API."""
    try:
        # P0-2(b) 未接线（后续项）：本实例仅用于「内部提示词增强」，且与下方 vision
        # 回答调用共享 general 语义，需 per-call 注入才能差异化，已列为后续项。
        model = get_llm_client(temperature=0.7)

        enhance_prompt = IMAGE_GENERATION_ENHANCE_PROMPT.format(user_query=user_query)
        enhance_messages = [{"role": "user", "content": enhance_prompt}]

        logger.info("Enhancing user prompt: {}", user_query)
        # P1-4（重要）：此处**故意不转发 config**——本调用点由白名单节点
        # create_image_query 触发，但产出的是「内部提示词增强」，并非面向用户的
        # 回答；转发 config 会让增强后的提示词逐 token 冒泡到前端。真正面向用户的
        # 回答调用在 vision 分支（llm.ainvoke(..., config=config)）。
        enhanced_response = await model.ainvoke(enhance_messages)
        enhanced_prompt = enhanced_response.content.strip()
        logger.info("Enhanced prompt: {}", enhanced_prompt)

        api_key = settings.IMAGE_GENERATION_API_KEY
        base_url = settings.IMAGE_GENERATION_BASE_URL
        model_name = settings.IMAGE_GENERATION_MODEL
        size = settings.IMAGE_GENERATION_SIZE

        if not api_key:
            logger.error("IMAGE_GENERATION_API_KEY not configured")
            return {"messages": [AIMessage(content="抱歉，图片生成服务配置不完整，无法生成图片。")]}

        api_url = f"{base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": model_name, "prompt": enhanced_prompt, "size": size}

        logger.info("Calling CogView-4 API: {}", api_url)

        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=payload, headers=headers, timeout=60) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error("CogView-4 API error: {} - {}", resp.status, error_text)
                    return {"messages": [AIMessage(content="抱歉，图片生成失败，请稍后再试。")]}
                result = await resp.json()

        if "data" not in result or len(result["data"]) == 0:
            logger.error("CogView-4 API returned no image data: {}", result)
            return {"messages": [AIMessage(content="抱歉，图片生成失败，请稍后再试。")]}

        image_url = result["data"][0].get("url", "")
        if not image_url:
            logger.error("CogView-4 API returned no image URL: {}", result)
            return {"messages": [AIMessage(content="抱歉，图片生成失败，请稍后再试。")]}

        logger.info("Image generated successfully: {}", image_url)

        subject = "测试数据主题"
        for keyword in ["民法典", "刑法", "劳动合同法", "道路交通安全法", "行政处罚法"]:
            if keyword in user_query:
                subject = keyword
                break

        success_message = IMAGE_GENERATION_SUCCESS_PROMPT.format(subject=subject)
        response_content = f"{success_message}\n\n图片链接: {image_url}"

        return {"messages": [AIMessage(content=response_content)]}

    except asyncio.TimeoutError:
        logger.error("CogView-4 API timeout")
        return {"messages": [AIMessage(content="抱歉，图片生成超时，请稍后再试。")]}
    except Exception as e:
        logger.error("Error generating image: {}", e, exc_info=True)
        return {"messages": [AIMessage(content="抱歉，图片生成过程中出现错误。")]}


async def create_image_query(state: AgentState, *, config: RunnableConfig) -> Dict[str, List[BaseMessage]]:
    """Handle image queries - recognition or generation."""
    logger.info("-----Handle Image Query-----")
    image_path = config.get("configurable", {}).get("image_path", None)
    user_query = state.messages[-1].content if state.messages else ""

    generation_keywords = ["生成", "画", "创建", "制作图片", "做一张", "给我一张", "来一张"]
    is_generation = any(keyword in user_query for keyword in generation_keywords)

    if is_generation and not image_path:
        logger.info("Image Generation Request: {}", user_query)
        return await _generate_image(user_query, state)

    if not image_path:
        logger.warning("User Upload Image Path is None for recognition")
        return {"messages": [AIMessage(content=IMAGE_NOT_FOUND_MESSAGE)]}

    # 2026-09（P43）：``image_path`` 与 ``file_path`` 同源——都来自请求体的
    # ``configurable``，即客户端可控。此前直接 ``Image.open(image_path)`` 并把像素
    # 送去 vision 模型，等于任意图片文件读取。合法值只能来自上传接口的返回值
    # （``UPLOAD_DIR/images/...``），故约束到 ``settings.UPLOAD_DIR`` 内不改正常流程。
    safe_image = resolve_within_root(settings.UPLOAD_DIR, image_path)
    if safe_image is None:
        logger.warning(
            "Rejected out-of-root image_path (must be under UPLOAD_DIR={}): {!r}",
            settings.UPLOAD_DIR, image_path,
        )
        return {"messages": [AIMessage(content=IMAGE_NOT_FOUND_MESSAGE)]}

    if not safe_image.exists():
        logger.warning("User Upload Image Not Found: {}", image_path)
        return {"messages": [AIMessage(content=IMAGE_NOT_FOUND_MESSAGE)]}

    api_key = settings.VISION_API_KEY
    base_url = settings.VISION_BASE_URL
    vision_model = settings.VISION_MODEL

    # 兼容旧配置：VISION_BASE_URL 若已含 /chat/completions（.env.example 曾如此写），
    # 下方会再拼一次导致 .../chat/completions/chat/completions → 404。
    if base_url:
        base_url = base_url.rstrip("/")
        for suffix in ("/chat/completions", "/v1/chat/completions"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
                break

    if not api_key or not base_url or not vision_model:
        logger.error("Vision Model Configuration Not Complete")
        return {"messages": [AIMessage(content=IMAGE_NOT_FOUND_MESSAGE)]}

    logger.info("Using Vision Model: {} to process image: {}", vision_model, safe_image)

    try:
        with Image.open(safe_image) as img:
            max_size = 1024
            width, height = img.size
            ratio = min(max_size / width, max_size / height)

            if width <= max_size and height <= max_size:
                resized_img = img
            else:
                new_width = int(width * ratio)
                new_height = int(height * ratio)
                resized_img = img.resize((new_width, new_height), Image.LANCZOS)

            img_byte_arr = io.BytesIO()
            if resized_img.mode != "RGB":
                resized_img = resized_img.convert("RGB")
            resized_img.save(img_byte_arr, format="JPEG", quality=85)
            img_byte_arr.seek(0)

            image_data = base64.b64encode(img_byte_arr.read()).decode("utf-8")

            logger.info("Image Compressed, Original Size: {}x{}, New Size: {}x{}",
                        width, height, resized_img.width, resized_img.height)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        payload = {
            "model": vision_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个专业的测试文档与证据材料图像分析助手。请仔细分析图片中的内容，特别关注文档类型、关键文字信息、签章与签署、参与方信息等细节，并说明其与测试数据集问题的关联。",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                        }
                    ],
                },
            ],
            "max_tokens": 4000,
            "temperature": 0.7,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    image_description = result["choices"][0]["message"]["content"]
                    logger.info("Successfully processed image and generated description")

                    # P0-2(b) 未接线（后续项）：image_handler 内多调用点共用同一
                    # llm 实例（增强 + 回答），需 per-node 注入才能差异化，已列为后续项。
                    llm = get_llm_client(temperature=0.7, tags=["image_query"])
                    system_prompt = GET_IMAGE_SYSTEM_PROMPT.format(image_description=image_description)
                    messages = [{"role": "system", "content": system_prompt}] + state.messages
                    # P1-4：面向用户的图片分析回答，转发 config → 逐 token 真流式。
                    response = await llm.ainvoke(messages, config=config)
                    return {"messages": [response]}
                else:
                    error_text = await response.text()
                    logger.error("Vision API Request Failed: {} - {}", response.status, error_text)
                    return {"messages": [AIMessage(content=IMAGE_NOT_FOUND_MESSAGE)]}

    except Exception as e:
        logger.error("Error processing image: {}", str(e))
        return {"messages": [AIMessage(content=IMAGE_NOT_FOUND_MESSAGE)]}
