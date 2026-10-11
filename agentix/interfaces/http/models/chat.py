from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field

from agentix.config.settings import settings

class ChatRequest(BaseModel):
    """聊天请求"""

    # 企业化 P2-4.5：统一长文本输入上限（QUERY_MAX_LENGTH，防 token 放大攻击）
    message: str = Field(
        ...,
        description="用户消息",
        min_length=1,
        max_length=settings.QUERY_MAX_LENGTH,
    )
    session_id: Optional[str] = Field(None, description="会话ID")
    user_id: Optional[str] = Field(None, description="用户ID")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "民法典关于合同违约是怎么规定的？",
                "session_id": "session_123",
                "user_id": "user_456",
            }
        }
    )


class ChatResponse(BaseModel):
    """聊天响应"""

    answer: str = Field(..., description="回答")
    session_id: str = Field(..., description="会话ID")
    type: str = Field(..., description="响应类型：knowledge/chat/reject/error")
    metadata: Optional[Dict[str, Any]] = Field(None, description="元数据")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "answer": "根据《民法典》第577条，当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任……",
                "session_id": "session_123",
                "type": "knowledge",
                "metadata": {
                    "route": "knowledge",
                    "confidence": 0.95,
                },
            }
        }
    )
