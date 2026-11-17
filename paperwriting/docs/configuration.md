# 配置与运行

## 配置来源

仅加载仓库根目录 `.env`，不搜索父目录。已存在的进程环境变量优先于 `.env`。不要复制作者原项目的配置；使用空白 `.env.example`。

首次安装后，应用默认不具备可用的生成模型，也不下载向量模型。可先打开网页确认空白编辑器，再配置模型。

## API 合约

`LLM_BASE_URL` 是 `/chat/completions` 前的地址。以下只是本机服务配置示意，不表示本项目附带该服务：

```dotenv
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=your-served-model-name
LLM_API_KEY=
```

自行启动本机推理服务，或填写云端供应商提供的 HTTPS 地址与密钥。无密钥时不发送 Authorization 请求头；需要鉴权的服务会拒绝请求。

适配器发送非流式请求，包含系统消息、用户消息、模型名、`max_tokens` 及 JSON object 输出要求。模型内容必须是一个 JSON 对象；输出截断、非法 JSON 或缺少必要字段会失败，不会伪装成成功。

可选响应字段 `usage.total_tokens` 用于记录用量；供应商未提供时记作 0。因此不能把本地 Token 统计当作所有供应商的账单。当前不支持按角色配置不同模型或其他供应商专有响应格式。

设置页“已配置”仅表示填写了基础地址与模型名，不代表 API 已通过连接验证。

## 向量检索

1. 安装 `requirements-embeddings.txt`。
2. 设置 `PAPERCODING_ENABLE_EMBEDDINGS=true`。
3. 选择 FastEmbed 支持的模型名。
4. 重启后添加自己的文献，再检索或重建索引。

下载和模型加载可能较慢。失败时混合检索返回明确警告并降级为关键词；仅向量模式报错。关闭开关不会删除已有缓存。切换模型后索引按模型命名空间隔离。

完全离线环境需要提前准备依赖和 FastEmbed 兼容的缓存。任意 Hugging Face 目录不能保证直接使用；没有预装权重时不要假设语义检索可离线启动。

## 数据路径

默认数据位于本仓库 `data/`，模型缓存位于 `.cache/embeddings/`。可以配置 `PAPERCODING_DATA_DIR` 和 `PAPERCODING_EMBEDDING_CACHE`，建议使用绝对路径。

每个数据目录仅启动一个服务。初始标题为“未命名论文”，章节标题是通用空目录，正文、研究方法、作者信息、文献、模板、图片、运行历史均为空。

## 常见问题

- **没有配置 API**：编辑和保存可用，生成/翻译任务会提示配置缺失。
- **DOCX 导出失败**：先上传自己的合法 DOCX 模板；空壳版不附带期刊文件。
- **扫描 PDF 无文字**：先自行 OCR，本项目不做 OCR。
- **端口占用**：使用 `python run.py --port 8502`。
- **macOS 安装依赖提示证书错误**：可尝试 `python -m pip install --cert /etc/ssl/cert.pem -r requirements.txt`，使用系统证书验证，不要关闭 TLS 校验。
- **返回非 JSON**：检查模型及服务是否支持约定的 JSON 输出；必要时改造适配器。
- **未启用语义检索**：选择关键词模式，或按上述步骤安装并启用向量模型。
