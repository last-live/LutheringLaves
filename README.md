### LutheringLaves 介绍
鸣潮国服客户端下载脚本，方便在SteamDeck下载鸣潮国服客户端，Linux其他发行版同样适用。

镜像仓库：https://gitee.com/tiz/LutheringLaves

#### 初次下载游戏
1.运行以下命令后，会在当前目录下载'LutheringLaves.py'文件并创建'Wuthering Waves Game'文件夹，游戏文件会保存在'Wuthering Waves Game'文件夹下。
``` bash
curl -sL https://raw.githubusercontent.com/last-live/LutheringLaves/main/LutheringLaves.py -o LutheringLaves.py && python3 LutheringLaves.py
```

国内镜像仓库命令，网络不好用这个
``` bash
curl -sL https://gitee.com/tiz/LutheringLaves/raw/main/LutheringLaves.py -o LutheringLaves.py && python3 LutheringLaves.py
```

2.下载完成后，将游戏客户端文件将"Wuthering Waves.exe"添加到steam库里。

3.steam中找到该游戏，游戏属性里打开"强制使用特定 Steam Play 兼容工具"，选择一个Porton，建议是使用最新版本的GE-Proton。

#### 更新游戏
更新游戏时，需要设定工作模式 --mode 为 update，请确保当前目录下有"Wuthering Waves.exe"文件，如需指定其它目录，可以指定参数 --folder
``` bash
curl -sL https://raw.githubusercontent.com/last-live/LutheringLaves/main/LutheringLaves.py -o LutheringLaves.py && python3 LutheringLaves.py --mode update
```

国内镜像仓库命令，网络不好用这个
``` bash
curl -sL https://gitee.com/tiz/LutheringLaves/raw/main/LutheringLaves.py -o LutheringLaves.py && python3 LutheringLaves.py --mode update
```

#### 设置下载目录
设置启动参数--folder，可以指定下载目录，目前只支持相对路径，默认目录是'Wuthering Waves Game'
``` bash
python3 LutheringLaves.py --mode install --folder gamefolder
```

### 增量更新
设置启动参数--mode为patch-update，可以使用增量更新下载，功能尚未完全测试通过，请谨慎使用。使用增量更新时，需要游戏目录下有launcherDownloadConfig.json文件且本地版本大于等于2.4.0。增量更新过程中，会产生临时文件，需要硬盘预留额外的空间，当前版本预计额外预留60g。
``` bash
python3 LutheringLaves.py --mode patch-update
```