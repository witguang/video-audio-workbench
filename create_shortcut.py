import os
import subprocess
import sys
import tempfile
import winreg


def get_real_desktop_path() -> str:
    """获取当前系统真实的桌面路径（完美兼容 OneDrive 重定向）。"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        desktop_raw, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        return os.path.expandvars(desktop_raw)
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Desktop")


def check_and_fix_ico(current_dir):
    """检测并修复 logo.ico 的文件格式，若为普通图片伪造后缀，可自动转换为标准 ICO 格式。"""
    ico_path = os.path.join(current_dir, "logo.ico")
    
    is_fake_ico = False
    if os.path.exists(ico_path):
        try:
            with open(ico_path, "rb") as f:
                head = f.read(4)
                # 标准 ICO 的前四个字节为：00 00 01 00
                if head != b'\x00\x00\x01\x00':
                    is_fake_ico = True
        except IOError:
            pass

    # 如果图标不存在或格式不合规，尝试将同级目录下的 PNG/JPG 转换为标准 ICO
    if not os.path.exists(ico_path) or is_fake_ico:
        possible_sources = ["logo.png", "logo.jpg", "logo.jpeg"]
        source_image = None
        for img_name in possible_sources:
            img_p = os.path.join(current_dir, img_name)
            if os.path.exists(img_p):
                source_image = img_p
                break
        
        # 如果 logo.ico 本身存在但其实是个 png，直接以它为源进行转码
        if is_fake_ico and not source_image:
            source_image = ico_path
            
        if source_image:
            try:
                from PIL import Image
                img = Image.open(source_image)
                # 导出符合 Windows 规范的多分辨率 ICO 图标
                img.save(
                    ico_path, 
                    format="ICO", 
                    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
                )
                print(f"检测到非标准格式图标，已成功将其转换为标准 Windows 多分辨率图标：{ico_path}")
            except ImportError:
                print("\n[提示] 检测到您的 logo.ico 文件格式似乎不标准（可能是直接重命名得到的伪ico）。")
                print("建议在终端运行 'pip install pillow' 安装图像处理库。")
                print("本脚本检测到该库后，将自动为您把普通的 PNG/JPG 无损转换成合规的 ICO 图标，彻底解决白块问题。")
            except Exception as e:
                print(f"尝试修复图标格式时发生未知异常: {e}")


def create_desktop_shortcut():
    current_dir = os.path.dirname(os.path.abspath(__file__))

    # 格式自检与自动转换
    check_and_fix_ico(current_dir)

    script_path = os.path.join(current_dir, "workbench_app.py")
    icon_path = os.path.join(current_dir, "logo.ico")

    # 如果还是不存在合规图标，回退至 pythonw.exe 自带的图标
    if not os.path.exists(icon_path):
        icon_path = sys.executable.replace("python.exe", "pythonw.exe")

    desktop_dir = get_real_desktop_path()
    shortcut_path = os.path.join(desktop_dir, "视频音频处理工作台.lnk")
    pythonw_path = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw_path):
        pythonw_path = sys.executable

    # 用 PowerShell 创建快捷方式（不用 VBS：VBS 临时文件按 GBK 写入、wscript 按
    # 系统 ANSI 解码，中文路径（如 OneDrive 桌面重定向）会乱码导致 80070003。
    # PowerShell 脚本用 UTF-8 BOM 写入，中文路径无歧义；-WindowStyle Hidden 无窗口。）
    ps1_content = (
        "$ws = New-Object -ComObject WScript.Shell\n"
        "$s = $ws.CreateShortcut($env:SPH_SC_PATH)\n"
        "$s.TargetPath = $env:SPH_SC_TARGET\n"
        "$s.Arguments = $env:SPH_SC_ARGS\n"
        "$s.WorkingDirectory = $env:SPH_SC_WORKDIR\n"
        "$s.IconLocation = $env:SPH_SC_ICON + \",0\"\n"
        "$s.Save()\n"
    )

    temp_ps1 = tempfile.NamedTemporaryFile(delete=False, suffix=".ps1",
                                           mode="w", encoding="utf-8-sig")
    try:
        temp_ps1.write(ps1_content)
        temp_ps1.close()

        env = dict(os.environ)
        env["SPH_SC_PATH"] = shortcut_path
        env["SPH_SC_TARGET"] = pythonw_path
        env["SPH_SC_ARGS"] = f'"{script_path}"'
        env["SPH_SC_WORKDIR"] = current_dir
        env["SPH_SC_ICON"] = icon_path

        CREATE_NO_WINDOW = 0x08000000  # 隐藏 PowerShell 自身窗口
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-File", temp_ps1.name],
            capture_output=True, env=env, creationflags=CREATE_NO_WINDOW)

        if result.returncode == 0 and os.path.isfile(shortcut_path):
            print(f"快捷方式已创建到桌面：{shortcut_path}")
        else:
            err = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
            print(f"创建快捷方式失败：{err or ('PowerShell 返回码 %s' % result.returncode)}")
    except Exception as e:
        print(f"运行过程中发生异常：{e}")
    finally:
        if os.path.exists(temp_ps1.name):
            try:
                os.remove(temp_ps1.name)
            except OSError:
                pass


if __name__ == "__main__":
    create_desktop_shortcut()