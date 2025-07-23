### LutheringLaves 介绍
鸣潮国服客户端下载脚本，方便在SteamDeck下载鸣潮国服客户端，Linux其他发行版同样适用。

### 使用方法
1.运行以下命令后，会在当前目录下载'LutheringLaves.py'文件并创建'Wuthering Waves Game'文件夹，游戏文件会保存在'Wuthering Waves Game'文件夹下。
``` bash
curl -sL https://raw.githubusercontent.com/last-live/LutheringLaves/main/LutheringLaves.py -o LutheringLaves.py && python3 LutheringLaves.py
```
2.下载完成后，将游戏客户端文件将"Wuthering Waves.exe"添加到steam库里。

3.steam中找到该游戏，游戏属性里打开"强制使用特定 Steam Play 兼容工具"，选择一个Porton，建议是使用最新版本的GE-Proton。

### 其他
当前脚本基于鸣潮国服启动器2.14.0版本抓包分析，下载的是2.4.1的游戏客户端，暂不确定版本更新后是否会失效，等2.5版本正式发布后再看看，2.5的预下载目前也不支持。