"""配置集中在一处，避免「某个模块自己读环境变量」的隐性依赖。"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 业务系统的地址。Agent 通过 HTTP 调它，不 import 它的代码——
# 这一条是「接系统」和「同进程假函数」的分界。
BIZ_BASE_URL = os.environ.get("BIZ_BASE_URL", "http://127.0.0.1:8000")

LLM_URL = os.environ.get("LLM_URL", "https://api.deepseek.com/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
LLM_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# 一轮对话最多几次工具调用。超过就不再加工具，让模型用已有信息收尾。
MAX_STEPS = int(os.environ.get("MAX_STEPS", "6"))
