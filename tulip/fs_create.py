# fs_create.py

from littlefs import lfs
import os
import sys

if(len(sys.argv)<2):
    print("Usage: python fs_create.py (amyboard,tulip) [flash|flash-notouch]")

distro = sys.argv[1]

# Ship editable copies of the built-in apps as /sys/ex/my_*.py -- the frozen
# originals are read-only, and the docs point users at these copies to edit.
# They are gitignored (tulip/fs/tulip/ex/my_*), so regenerate them per build.
if(distro=='tulip'):
    import shutil
    for app in ('drums', 'juno6', 'voices', 'worldui'):
        shutil.copyfile('shared/py/%s.py' % (app), 'fs/tulip/ex/my_%s.py' % (app))

if(distro=='tulip'):
    os.chdir('esp32s3')
else:
    os.chdir('amyboard')

idf_path = os.environ["IDF_PATH"]  # get value of IDF_PATH from environment
parttool_dir = os.path.join(idf_path, "components", "partition_table")  # parttool.py lives in $IDF_PATH/components/partition_table

sys.path.append(parttool_dir)  # this enables Python to find parttool module
from parttool import *  # import all names inside parttool module
import gen_esp32part as gen

if(not os.path.exists('build/flash_args')):
    print("Run this after a successful build only")
    sys.exit()

SYSTEM_HOME = "../fs/%s" % (distro)

# Copy over only these extensions (compared case-insensitively, so .MID
# and .mid both match).
good_exts = [".txt", ".png", ".py", ".json", ".obj", ".wav", ".mid"]
# And these folders
source_folders = ['app','ex','im']

# Get the partition info from the built partition table
partition_table_file = "build/partition_table/partition-table.bin"
with open(partition_table_file, 'rb') as f:
    partition_table = gen.PartitionTable.from_binary(f.read())
vfs_partition = partition_table.find_by_name('vfs')
sys_partition = partition_table.find_by_name('system')

def copy_to_lfs(source, dest):
    #print("Copying %s to %s" % (source, dest))
    source_data = open(source, "rb").read()
    fh = lfs.file_open(fs, dest, "wb")
    lfs.file_write(fs, fh, source_data)
    lfs.file_close(fs, fh)

# First make an empty VFS for the user filesystem
cfg = lfs.LFSConfig(block_size=4096, block_count = int(vfs_partition.size / 4096),  disk_version=0x00020000)
fs = lfs.LFSFilesystem()
lfs.format(fs, cfg)
lfs.mount(fs, cfg)
copy_to_lfs('boot.py', 'boot.py')

print("writing VFS .bin file...")
with open("build/%s-vfs.bin" % (distro),"wb") as fh:
    fh.write(cfg.user_context.buffer)
print("... done.")

cur_dir = os.getcwd()
os.chdir(SYSTEM_HOME)
folders = [x[0][2:] for x in os.walk('.')][1:]

cfg = lfs.LFSConfig(block_size=4096, block_count = int(sys_partition.size / 4096),  disk_version=0x00020000)
fs = lfs.LFSFilesystem()
lfs.format(fs, cfg)
lfs.mount(fs, cfg)

for folder in folders:
    lfs.mkdir(fs,folder)
    for file in os.listdir(folder):
        file_part, ext = os.path.splitext(file)
        if(ext.lower() in good_exts):
            copy_to_lfs(folder+'/'+file, folder+'/'+file)

os.chdir(cur_dir)

print("writing sys .bin file...")
with open("build/%s-sys.bin" % (distro),"wb") as fh:
    fh.write(cfg.user_context.buffer)
print("... done.")


# Gamma9001 drum banks: generate drums.bin from the amy submodule and flash it
# into the `drums` partition (AMY mmaps it at boot; see amy_connector.c).
drums_partition = None
try:
    drums_partition = partition_table.find_by_name('drums')
except Exception:
    pass
if drums_partition is not None:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'amy.headers', 'gamma9001'], cwd='../../amy')
    drums_bin = open('../../amy/build/drums.bin', 'rb').read()
    if len(drums_bin) > drums_partition.size:
        raise SystemExit("drums.bin (%d bytes) does not fit the drums partition (%d bytes)"
                         % (len(drums_bin), drums_partition.size))
    with open('build/%s-drums.bin' % (distro), 'wb') as fh:
        fh.write(drums_bin)
    print("drums.bin: %d bytes into %s partition at %s" % (
        len(drums_bin), 'drums', hex(drums_partition.offset)))

# Update the flash_args file to have the sys and user partitions
flash_args = open('build/flash_args','r').read().split('\n')[:-1]
flash_args.append('%s %s-sys.bin' % (hex(sys_partition.offset), distro))
flash_args.append('%s %s-vfs.bin' % (hex(vfs_partition.offset), distro))
if drums_partition is not None:
    flash_args.append('%s %s-drums.bin' % (hex(drums_partition.offset), distro))
new_flash_args = open('build/flash_args_%s' % (distro),'w')
for f in flash_args:
    new_flash_args.write('%s\n' % (f))
new_flash_args.close()
os.chdir('build')
os.system('esptool.py --chip esp32s3 merge_bin -o %s.bin @flash_args_%s' % (distro, distro))
os.chdir('..')

# I don't love this but it works
# i wonder if i can get CMake to pass along MICROPY_BOARD to this program in a shell instead
MICROPY_BOARD = subprocess.check_output("grep MICROPY_BOARD build/CMakeCache.txt | cut -d '=' -f2 | awk '{print $1}'",shell=True)[:-1].decode('ascii')
os.system("mkdir -p dist")
os.system("cp build/%s.bin dist/%s-full-%s.bin" % (distro, distro, MICROPY_BOARD))
os.system("cp build/micropython.bin dist/%s-firmware-%s.bin" % (distro, MICROPY_BOARD))
os.system("cp build/%s-sys.bin dist/%s-sys.bin" %(distro, distro))

# Optionally do the flash of the whole image
#   flash          - normal: CYNTHIA does the 1200bps touch to enter the
#                    bootloader automatically; other boards use default_reset.
#   flash-notouch  - skip the touch entirely and flash with --before no_reset.
#                    Use this when the board is ALREADY in the ROM bootloader
#                    (e.g. you held the BOOT button while plugging in / resetting,
#                    or a previous touch left it there). Works on any board.
if(len(sys.argv)>2):
    if(sys.argv[2] in ('flash', 'flash-notouch')):
        no_touch = (sys.argv[2] == 'flash-notouch')
        print("Writing full image%s" % (" (no-touch mode)" if no_touch else ""))
        # CYNTHIA is a native-USB-CDC board (TinyUSB, no UART auto-reset circuit).
        # esptool's default_reset/usb_reset strategies toggle DTR/RTS, which only
        # works with the ESP32-S3's built-in USB-JTAG-Serial ROM peripheral -- not
        # with TinyUSB CDC. CYNTHIA's firmware instead implements the Arduino-style
        # 1200bps touch (MICROPY_HW_USB_CDC_1200BPS_TOUCH): opening the CDC port at
        # 1200 baud and closing it makes the firmware jump to the ROM bootloader.
        # Do that touch here, wait for the ROM bootloader to re-enumerate, then
        # flash with --before no_reset (the chip is already in the bootloader).
        if(MICROPY_BOARD == 'CYNTHIA' and not no_touch):
            import serial
            import serial.tools.list_ports
            import time as _time

            def _find_cynthia_port():
                # The CDC REPL port is the one whose interface name contains CDC.
                # MicroPython's TinyUSB descriptor exposes CDC + MIDI; the CDC
                # interface is what we want. Fall back to any usbmodem port.
                candidates = []
                for p in serial.tools.list_ports.comports():
                    if p.device.startswith('/dev/cu.'):
                        candidates.append(p)
                # Prefer a port whose description/interface hints at CDC/MicroPython.
                for p in candidates:
                    desc = (p.description or '') + ' ' + (p.interface or '')
                    if 'CDC' in desc.upper() or 'MICROPYTHON' in desc.upper() or 'SERIAL' in desc.upper():
                        return p.device
                # Otherwise pick the first usbmodem (CDC) port.
                for p in candidates:
                    if 'usbmodem' in p.device:
                        return p.device
                return None

            port = os.environ.get('ESPPORT') or _find_cynthia_port()
            if port is None:
                print("CYNTHIA: no CDC serial port found; plug the board in and retry.")
                sys.exit(1)
            print("CYNTHIA: touching %s at 1200 baud to enter bootloader..." % port)
            try:
                ser = serial.Serial(port, baudrate=1200, timeout=1)
                ser.setDTR(False)
                ser.setRTS(False)
                ser.close()
            except Exception as e:
                print("CYNTHIA: 1200bps touch failed: %s" % e)
                sys.exit(1)
            # The chip resets and re-enumerates as the ROM bootloader. Give it time.
            _time.sleep(2.0)
            # Flash with no_reset: the chip is already in the ROM bootloader.
            os.system("esptool.py --before no_reset write_flash 0x0 dist/%s-full-%s.bin" % (distro, MICROPY_BOARD))
        elif(no_touch):
            # flash-notouch: assume the chip is already in the ROM bootloader
            # (BOOT held at reset, or a previous touch). No port probing, no
            # reset strategy -- just talk to whatever is there. Set ESPPORT to
            # pin the port if more than one serial device is attached.
            port_arg = ("-p %s " % os.environ['ESPPORT']) if os.environ.get('ESPPORT') else ""
            os.system("esptool.py %s--before no_reset write_flash 0x0 dist/%s-full-%s.bin" % (port_arg, distro, MICROPY_BOARD))
        else:
            os.system("esptool.py write_flash 0x0 dist/%s-full-%s.bin" % (distro, MICROPY_BOARD))

os.chdir('..')

