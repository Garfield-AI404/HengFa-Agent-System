"""文件和 payload 哈希（规格 2.6 files/hashing.py，6.5/6.3 规则 7）。

- 上传文件保留 sha256 文件哈希（6.4 步骤 3：保留文件哈希；6.5 checkpoint
  保存文件哈希，不保存无关工作表内容）；
- payload hash 供 D20 复核（review payload hash）复用，本单元只提供
  sha256 纯函数，不超前实现 D20 逻辑。
"""

from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    """字节内容 → sha256 十六进制（文件哈希；6.4 步骤 3 / 6.5）。"""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    """按路径读取文件内容计算 sha256（流式，不整读大文件）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
