"""请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FragmentIn(BaseModel):
    """客户端提交的字幕片段。"""

    seq: int = Field(..., ge=1, description="字幕序号，从 1 开始")
    revision: int = Field(..., ge=0, description="修订号，同序号仅接受更高值")
    text: str = Field(..., description="字幕文本")


class ReleasedFragment(BaseModel):
    seq: int
    revision: int
    text: str


class SubmitResponse(BaseModel):
    channel: str
    seq: int
    accepted: bool = Field(..., description="本次提交是否生效")
    watermark: int = Field(..., description="提交后的最新水位线")
    released: list[ReleasedFragment] = Field(
        default_factory=list, description="本次提交触发定稿的连续片段"
    )


class FinalizedFragment(BaseModel):
    seq: int
    revision: int
    text: str
    finalized_at: str


class FinalizedResponse(BaseModel):
    channel: str
    watermark: int
    fragments: list[FinalizedFragment]


class ChannelStatus(BaseModel):
    channel: str
    watermark: int
    pending: int = Field(..., description="暂存区等待缺口补齐的片段数")
    finalized: int = Field(..., description="已定稿片段总数")
