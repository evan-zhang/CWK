# RT-046 格式工厂 v1.1（转换器表 + 回执三链 + 无后缀嗅探）

- 目标：`decide_format` 升级为逐格式转换器表（蓝本：bd-eval-loop RT-108），让投前库 43 件 placeholder 中的可转件变为 converted 正文可问答。
- 转换器表（初版）：docx → anydoc（pin 0.1.9）；xlsx/xlsm → markitdown（pin 0.1.7，公式缓存值不丢）；pdf → anydoc 纯文本；md/txt → 直通不变。
- 无后缀文件：magic-bytes 嗅探（PNG/JPEG/GIF、ZIP 容器→docx|xlsx、%PDF）再入表——bd-eval-loop 未覆盖、本 RT 自补。
- 回执三链：provenance 账本记 converter{name,version} + 源 sha256 + 产物 sha256；转换必须确定性，禁止交 LLM Agent 转换。
- 依赖红线：anydoc/markitdown 为可选 extra（同 openpyxl 先例），缺失时降级 placeholder 不许坏基线；纯标准库底线不动。
- 图片：旁路识别失败不阻断正文；v1.1 不做视觉识别，只留结构位。
- 验收：①投前库 43 件 placeholder 重摄取后 reconcile 零缺件且 placeholder 数下降、转换件引文可经网关实时拉取；②单测锁转换器表与嗅探分支；③三门禁绿。
- 反空转：转换器缺失必降级并记账（不静默跳过）；「转出 0 字」按失败处理进回执（bd-eval-loop DI-006 教训）。
