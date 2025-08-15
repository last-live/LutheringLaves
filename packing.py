import sys
import os
import subprocess
from pathlib import Path
import platform

def build_with_nuitka(target_platform=None):
    """
    使用Nuitka打包LutheringLaves项目
    """
    # 获取项目根目录
    project_root = Path(__file__).parent.absolute()
    
    # 设置工作目录
    os.chdir(project_root)
    
    # 确定目标平台
    if target_platform is None:
        target_platform = platform.system().lower()
    
    # 构建GUI版本
    cmd_gui = [
        sys.executable, "-m", "nuitka",
        "--standalone",           # 独立打包
        "--output-dir=dist",      # 输出目录
        "--enable-plugin=pyside6", # 启用PySide6插件
        "LutheringLavesLauncher.py"
    ]
    
    # 根据目标平台添加特定选项
    if target_platform == "windows":
        cmd_gui.extend([
            "--windows-console=disable",   # 不显示控制台窗口
            "--windows-icon-from-ico=resource/launcher.ico",  # 设置图标
        ])
    
    # 添加资源文件
    if os.path.exists("resource"):
        cmd_gui.append("--include-data-dir=resource=resource")           # 包含资源文件
    
    if os.path.exists("Font"):
        cmd_gui.append("--include-data-dir=Font=Font")                   # 包含字体文件
        
    if os.path.exists("tools"):
        cmd_gui.append("--include-data-dir=tools=tools")           # 包含资源文件
    
    print(f"开始打包 {target_platform.upper()} GUI版本...")
    print(f"执行命令: {' '.join(cmd_gui)}")
    
    try:
        result = subprocess.run(cmd_gui, check=True, capture_output=True, text=True)
        print(f"{target_platform.upper()} GUI版本打包成功!")
        print("输出目录: dist/")
    except subprocess.CalledProcessError as e:
        print(f"{target_platform.upper()} GUI版本打包失败:")
        print(f"错误信息: {e}")
        print(f"stderr: {e.stderr}")
        return False
    
    return True

def build_cli_version(target_platform=None):
    """
    打包命令行版本
    """
    # 获取项目根目录
    project_root = Path(__file__).parent.absolute()
    
    # 设置工作目录
    os.chdir(project_root)
    
    # 确定目标平台
    if target_platform is None:
        target_platform = platform.system().lower()
    
    # 构建CLI版本
    cmd_cli = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--onefile",
        "--output-dir=dist",
        "LutheringLaves.py"
    ]
    
    print(f"开始打包 {target_platform.upper()} CLI版本...")
    print(f"执行命令: {' '.join(cmd_cli)}")
    
    try:
        result = subprocess.run(cmd_cli, check=True, capture_output=True, text=True)
        print(f"{target_platform.upper()} CLI版本打包成功!")
        print("输出目录: dist/")
    except subprocess.CalledProcessError as e:
        print(f"{target_platform.upper()} CLI版本打包失败:")
        print(f"错误信息: {e}")
        print(f"stderr: {e.stderr}")
        return False
    
    return True

def build_cross_platform():
    """
    打包跨平台版本（Linux和Windows）
    """
    print("开始打包跨平台版本...")
    
    # 打包Windows版本
    success_win_gui = build_with_nuitka("windows")
    success_win_cli = build_cli_version("windows")
    
    # 打包Linux版本
    success_linux_gui = build_with_nuitka("linux")
    success_linux_cli = build_cli_version("linux")
    
    return success_win_gui and success_win_cli and success_linux_gui and success_linux_cli

if __name__ == "__main__":
    print("LutheringLaves Nuitka 打包脚本")
    print("=" * 40)
    
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        # 只打包CLI版本
        success = build_cli_version()
    elif len(sys.argv) > 1 and sys.argv[1] == "--both":
        # 打包两个版本（当前平台）
        success_gui = build_with_nuitka()
        success_cli = build_cli_version()
        success = success_gui and success_cli
    elif len(sys.argv) > 1 and sys.argv[1] == "--cross-platform":
        # 打包跨平台版本
        success = build_cross_platform()
    else:
        # 默认只打包GUI版本
        success = build_with_nuitka()
    
    if success:
        print("\n打包完成!")
    else:
        print("\n打包过程中出现错误!")
        sys.exit(1)