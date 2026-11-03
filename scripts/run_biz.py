"""启动业务系统。PyCharm 里右键 Run 即可，不用记 uvicorn 命令。

不用 `uvicorn.run("biz.app:app")` 那种字符串写法：那样 uvicorn 会自己
再 import 一次，而直接跑脚本时 `sys.path[0]` 是 scripts/，找不到 `biz`。
这里先把项目根塞进 path，再传 app 对象——不依赖工作目录，也不依赖 reload。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from biz.app import app  # noqa: E402

if __name__ == "__main__":
    print("业务系统启动中：http://127.0.0.1:8000   接口文档 /docs")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
