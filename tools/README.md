# 工程工具

- build.py：Qt、HLS C仿真、综合与完整Vivado重建。
- run.py：启动当前界面，自动指定后端与Python；Qt运行库路径可通过 --qt-bin 传入。
- verify.py：无板测试，不连接摄像头或开发板。
- package.py：使用RAR生成小于200 MB的代码包，验证完整性、解压文件集合与哈希。

所有工具从自身位置定位仓库，可从任意当前目录调用。命令和依赖见根README与docs/reproduction.md。工具安装位置通过PATH或CLI传入，构建和运行产物统一写outputs。
