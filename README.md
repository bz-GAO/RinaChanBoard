# <center>  Rina-Chan-Board </center>

这是一个基于 Streamlit 与大语言模型构建的本地可视化交互桌面助手。项目尝试还原了《虹咲学园学园偶像同好会》中天王寺璃奈的“璃奈板”设定，并集成了完整的工程化对话系统。

##  核心特性
- **高度定制化人格**：内置系统级 Prompt，严格遵循角色设定输出（支持颜文字情绪表达）。
- **工程化上下文管理**：实现了基于滚动摘要（Rolling Summary）的 Token 截断与记忆压缩算法，支持超长对话。
- **本地多模态解析**：支持直接上传读取 PDF、Word、PPT、Excel 以及图像文件。
- **Agent 工具链**：接入 Tavily 搜索引擎与 OpenAI 图像生成接口，实现联网查证与文本生图。
- **对话持久化**：具备完善的 JSON 格式对话导出、校验与离线载入机制。

##  快速启动
1. 确保安装了 Python 3.10+ 环境。
2. 安装依赖：`pip install -r requirements.txt`
3. 复制 `.env.example` 并重命名为 `.env`，填入你的 API 密钥。
4. Windows 用户双击 `RinaChanBoard.bat` 即可无黑框静默运行；或在终端执行 `python rina_launch.pyw`。