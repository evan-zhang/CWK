#!/bin/sh
# fixture wrapper：以 -S 禁用 site-packages，使 `import yaml` 必然 ImportError，
# 用于验证 rt-guard.sh 在无 PyYAML 环境走受限子集解析器的兜底路径。
exec python3 -S "$@"
