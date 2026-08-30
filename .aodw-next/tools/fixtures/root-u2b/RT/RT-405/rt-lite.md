# RT-405 fixture（仓库外引用豁免）

上游实现位于 pre_commit/commands/install_uninstall.py:51（第三方包内文件，仓库内无候选，skip 不报）；
另有绝对路径引用 /opt/external/tool.py:12（抽取层即不匹配，天然豁免）。
