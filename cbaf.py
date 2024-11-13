#!/data/data/com.termux/files/usr/bin/python
import io
import json
import logging
import random
import re
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import PIL
import cv2
import numpy
import numpy as np
import pexpect
import pytesseract
import requests
from PIL import Image
from discord_webhook import DiscordWebhook

"""
customization: change the coords in one of the following functions:
    start_battle()
    deploy_troops()
    collect_bb_elixir()

most of the {left,right,top,bottom}lim values are done by eyeballing fyi
"""

droidcast = "com.rayworks.droidcast"
clashofclans = 'com.supercell.clashofclans'
screenshot_url = ''
droidcast_started = False
droidcast_port = 53516

bb_cart_collect_button = cv2.imread('assets/cart_collect_button.jpg')
bb_cart_close_button = cv2.imread('assets/cart_close_button.jpg')

with open('config.json', 'r') as cff:
    config = json.loads(cff.read(), object_hook=lambda d: SimpleNamespace(**d))

logging.basicConfig(format='[%(levelname)s] [%(asctime)s] %(msg)s', level=logging.INFO)

screen_width = -1
screen_height = -1
oo = 999
shell = pexpect.spawn('su -c sh')


# region adb and utils

class Minitouch:
    mt = None

    def __init__(self):
        (rc, out, err) = run_command_piped(['getprop', 'ro.product.cpu.abi'])
        abi = out.strip()

        self.mt = pexpect.spawn(f'su -c minitouch/{abi}/minitouch -i')
        time.sleep(0.1)

    def __del__(self):
        self.mt.terminate()

    def send(self, command, commit=True, wait=.0):
        self.mt.sendline(command)
        if commit:
            self.mt.sendline('c')
        if wait > 0:
            time.sleep(wait)


def run_command(command):
    r = random.randint(2 ** 15, 2 ** 16)
    command = ' '.join([str(x) for x in command]) + f' && echo {r}'
    shell.sendline(command)
    shell.expect(str(r))


def send_webhook(msg: str):
    def t(m: str):
        whk = DiscordWebhook(url=config.reminder.webhook, content=m)
        whk.execute()

    th = threading.Thread(target=t, args=(msg,))
    th.start()


def handler(signum, frame):
    # # run_command_piped(["forward", "--remove", "tcp:%d" % droidcast_port])
    # shell.terminate()
    # shell.close()

    exit(1)


def run_command_piped(args, pipeOutput=True, wait=False, silent=False):
    # args = adb + ['-s', device] + args
    if args[0] == 'shell':
        del args[0]
    args = ['su', '-c'] + args

    # print('exec cmd : %s' % args)
    out = None
    if pipeOutput:
        out = subprocess.PIPE

    p = subprocess.Popen([str(arg) for arg in args],
                         stdout=out if not silent else subprocess.DEVNULL,
                         stderr=None if not silent else subprocess.DEVNULL,
                         encoding='utf-8')
    stdout, stderr = p.communicate()
    if wait:
        p.wait()
    return p.returncode, stdout, stderr


def landscape_resolution() -> tuple[int, int]:
    global screen_width, screen_height
    if screen_height == -1:
        (rc, out, err) = run_command_piped(['wm', 'size'])
        w, h = str(out).splitlines()[-1].removeprefix('Override size: ').removeprefix('Physical size: ').split('x')
        w = int(w)
        h = int(h)
        screen_width = max(w, h)
        screen_height = min(w, h)

    return screen_width, screen_height


def screenshot() -> Image:
    res = requests.get(f'{screenshot_url}/screenshot')
    return Image.open(io.BytesIO(res.content))


def start_droidcast():
    def locate_apk_path():
        (rc, out, _) = run_command_piped(["pm",
                                          "path",
                                          droidcast])
        if rc or out == "":
            raise RuntimeError(
                "Locating apk failure, have you installed the app successfully?")

        prefix = "package:"
        postfix = ".apk"
        beg = out.index(prefix, 0)
        end = out.rfind(postfix)

        return "CLASSPATH=" + out[beg + len(prefix):(end + len(postfix))].strip()

    def print_url():
        global screenshot_url
        # (rc, out, _) = run_command_piped(["ip route | awk '/wlan*/{ print $9 }'| tr -d '\n'"])
        screenshot_url = f'http://localhost:{droidcast_port}'
        logging.info(f'droidcast started at {screenshot_url}')

    def automate():
        global droidcast_started

        try:
            class_path = locate_apk_path()
            print_url()

            args = ['su',
                    '-c',
                    class_path,
                    "app_process",
                    "/",  # unused
                    f"{droidcast}.Main",
                    "--port=%d" % droidcast_port]

            droidcast_started = True
            run_command_piped(args, silent=True)

        except Exception as e:
            print(e)

    automate()


def click(x, y):
    # logging.infoging.debug(f'click {x=} {y=}')
    run_command(['input', 'touchscreen', 'tap', x, y])
    # minitouch.send(f'd 0 {x} {y} 50', wait=0.025)
    # minitouch.send('u 0')


def swipe_from_center(distance, xcoef, ycoef, duration=200):
    w, h = landscape_resolution()
    x = w // 2
    y = h // 2

    run_command([
        'input',
        'touchscreen',
        'swipe',
        x,
        y,
        x + int(distance * xcoef),
        y + int(distance * ycoef),
        duration
    ])


# endregion


# region ocr & image recognition
def restrict_color(img: PIL.Image.Image, redrange: tuple[int, int], greenrange: tuple[int, int],
                   bluerange: tuple[int, int]) -> PIL.Image.Image:
    pixdata = img.load()
    for y in range(img.size[1]):
        for x in range(img.size[0]):
            r, g, b = pixdata[x, y]
            if redrange[0] < r < redrange[1] and greenrange[0] < g < greenrange[1] and bluerange[0] < b < bluerange[1]:
                pixdata[x, y] = (255, 255, 255)
            else:
                pixdata[x, y] = (0, 0, 0)

    return img


def center_ocr_boxes(boxes) -> tuple[int, int]:
    table = [line.split() for line in str(boxes).splitlines()]
    mid = table[len(table) // 2]
    char, x1, y1, x2, y2, alpha = mid
    x1 = int(x1)
    x2 = int(x2)
    y1 = int(y1)
    y2 = int(y2)
    return (x1 + x2) // 2, (y1 + y2) // 2


def ocr_boxes_to_str(boxes) -> str:
    firstentries = [line.split()[0] for line in str(boxes).splitlines()]
    return ''.join(firstentries)


def scan_image(src, target, leftlim=0.0, rightlim=1.0, toplim=0.0, bottomlim=1.0) -> tuple[int, int]:
    heat_map = cv2.matchTemplate(src, target, cv2.TM_CCOEFF_NORMED)
    y, x = np.unravel_index(np.argmax(heat_map), heat_map.shape)
    ih, iw, _ = src.shape
    if iw * leftlim < x < iw * rightlim and ih * toplim < y < ih * bottomlim:
        return x, y
    else:
        return -1, -1


def pil2cv(img):
    return cv2.cvtColor(numpy.array(img), cv2.COLOR_RGB2BGR)


def crop_size(w, h, obj):
    return (
        int(w * obj.x1),
        int(h * obj.y1),
        int(w * obj.x2),
        int(h * obj.y2)
    )


# endregion


def kill_game():
    logging.info(f'killing {clashofclans}')
    run_command_piped(['killall', '-9', clashofclans])


def start_game():
    logging.info(f'starting {clashofclans}')
    run_command_piped(['monkey', '-p', clashofclans, '-c', 'android.intent.category.LAUNCHER', '--pct-syskeys', 0, 1])


def attack_button_pos() -> tuple[bool, int, int]:
    img = screenshot()
    w, h = img.width, img.height
    img = img.crop(crop_size(w, h, config.ocr.attack_text))
    img = restrict_color(img, (245, oo), (245, oo), (245, oo))

    try:
        boxes = pytesseract.image_to_boxes(img)
        button = ocr_boxes_to_str(boxes)
        center = center_ocr_boxes(boxes)
        return 'tack' in button.lower(), int(w * config.ocr.attack_text.x1) + center[0], int(
            h * config.ocr.attack_text.y1) + center[1]
    except Exception:
        return False, -1, -1


def start_battle():
    def wait_attack_button() -> tuple[int, int]:
        found, x, y = attack_button_pos()
        while not found:
            found, x, y = attack_button_pos()
        return x, y

    def wait_match_button() -> tuple[int, int]:
        def find_match_button_pos() -> tuple[bool, int, int]:
            img = screenshot()
            w, h = img.width, img.height
            img = img.crop(crop_size(w, h, config.ocr.find_now_text))
            img = restrict_color(img, (245, oo), (245, oo), (245, oo))

            try:
                boxes = pytesseract.image_to_boxes(img)
                button = ocr_boxes_to_str(boxes)
                center = center_ocr_boxes(boxes)
                return 'find' in button.lower(), int(w * config.ocr.find_now_text.x1) + center[0], int(
                    h * config.ocr.find_now_text.y1) + center[1]
            except Exception:
                return False, -1, -1

        found, x, y = find_match_button_pos()
        while not found:
            found, x, y = find_match_button_pos()

        return x, y

    logging.info('waiting for game to fully load')

    ax, ay = wait_attack_button()
    logging.info('opening attack menu')
    click(ax, ay)

    mx, my = wait_match_button()
    logging.info('starting battle')
    click(mx, my)


def can_deploy_troops() -> bool:
    img = screenshot()
    w, h = img.width, img.height
    img = img.crop(crop_size(w, h, config.ocr.battle_start_in_text))
    img = restrict_color(img, (240, oo), (200, 225), (200, 225))

    pat = re.compile('Battle s.ar..?s in:')

    try:
        ocr = str(pytesseract.image_to_string(img)).splitlines()
        lines = [ll for ll in ocr if len(ll) > 0 and bool(pat.match(ll))]
        return len(lines) > 0
    except Exception:
        return False


def deploy_troops(stage2=False):
    while not can_deploy_troops():
        continue

    logging.info('deploying troops')

    card_width = config.battle.card_width
    start_x = config.battle.start_x
    start_y = config.battle.start_y
    deploy_x = 1
    deploy_y = screen_height // 2

    if stage2:
        deploy_x = config.battle.deploy_stage2.x
        deploy_y = config.battle.deploy_stage2.y

    for _ in range(config.battle.troop_slots):
        click(start_x, start_y)
        click(deploy_x, deploy_y)
        start_x += card_width
        time.sleep(0.05)


def collect_elixir_cart():
    def cart_close_button_pos():
        return scan_image(pil2cv(screenshot()), bb_cart_close_button, leftlim=0.7, rightlim=0.85, toplim=0.05,
                          bottomlim=0.2)

    def cart_collect_button_pos():
        return scan_image(pil2cv(screenshot()), bb_cart_collect_button, leftlim=0.6, rightlim=0.8, toplim=0.7,
                          bottomlim=0.9)

    logging.info('wait game load')

    f, x, y = attack_button_pos()
    while not f:
        f, x, y = attack_button_pos()

    logging.info('collecting elixir cart')

    cart_x = config.cart.x
    cart_y = config.cart.y

    swipe_from_center(config.cart.swipe_y, 0, 1)
    time.sleep(0.5)
    click(cart_x, cart_y)

    closex, closey = cart_close_button_pos()
    while closex < 0 or closey < 0:
        closex, closey = cart_close_button_pos()

    collectx, collecty = cart_collect_button_pos()
    if collectx > 0 and collecty > 0:
        click(collectx, collecty)

    click(closex, closey)


def find_return_home_pos() -> tuple[bool, int, int]:
    img = screenshot()
    w, h = img.width, img.height
    img = img.crop(crop_size(w, h, config.ocr.return_home_text))
    img = restrict_color(img, (245, oo), (245, oo), (245, oo))

    try:
        boxes = pytesseract.image_to_boxes(img)
        button = ocr_boxes_to_str(boxes)
        center = center_ocr_boxes(boxes)
        return 'retur' in button.lower() or 'home' in button.lower(), int(w * config.ocr.return_home_text.x1) + center[0], int(
            h * config.ocr.return_home_text.y1) + center[1]
    except Exception:
        return False, -1, -1


# 'return_home', 'stage2' or 'retry'
def return_home_or_stage_2() -> tuple[str, int, int]:
    r = 'retry', -1, -1
    if can_deploy_troops():
        r = ('stage2', 0, 0)

    rf, rx, ry = find_return_home_pos()
    if rf:
        r = ('return_home', rx, ry)

    return r


def main():
    global screenshot_url
    screenshot_url = f'http://localhost:{droidcast_port}'
    landscape_resolution()

    runs = 1

    while True:
        logging.info(f'==================> battle #{runs} <==================')
        start = time.time()

        kill_game()
        start_game()
        if runs % config.reminder.frequency == 0:
            t = config.reminder.duration
            send_webhook(f'<@{config.reminder.uid}> you have {t}s to use the resources')
            time.sleep(t)
            runs += 1
            continue

        if runs % config.cart.frequency == 0:
            collect_elixir_cart()
        start_battle()
        deploy_troops()

        logging.info('wating for stage2 or home')

        stat, x, y = return_home_or_stage_2()
        while stat == 'retry':
            stat, x, y = return_home_or_stage_2()

        if stat == 'return_home':
            pass
        elif stat == 'stage2':
            time.sleep(2)
            swipe_from_center(500, 1, -1, 100)
            swipe_from_center(600, 0, 1, 1000)
            deploy_troops(stage2=True)

            stat, x, y = return_home_or_stage_2()
            while stat != 'return_home':
                stat, x, y = return_home_or_stage_2()
            pass

        duration = round(time.time() - start, 2)
        logging.info(f'battle #{runs} took {duration}s')
        send_webhook(f'battle #{runs} took `{duration}`s')
        runs += 1
        time.sleep(config.sleep.duration if runs % config.sleep.frequency == 0 else 0)

    # while True:
    #     logging.info(f'==================> battle #{runs} <==================')
    #     start = time.time()
    #
    #     kill_game()
    #     start_game()
    #     if runs % config.reminder.frequency == 0:
    #         t = config.reminder.duration
    #         send_webhook(f'<@{config.reminder.uid}> you have {t}s to use the resources')
    #         time.sleep(t)
    #         runs += 1
    #         continue
    #
    #     if runs % config.cart.frequency == 0:
    #         collect_elixir_cart()
    #     start_battle()
    #     deploy_troops()
    #
    #     duration = round(time.time() - start, 2)
    #     logging.info(f'battle #{runs} took {duration}s')
    #     send_webhook(f'battle #{runs} took `{duration}`s')
    #     runs += 1
    #     time.sleep(config.sleep.duration if runs % config.sleep.frequency == 0 else 0)


if __name__ == '__main__':
    if len(sys.argv) == 1:
        main()
    elif sys.argv[1] == 'test':
        kill_game()
        start_game()
        time.sleep(10)
        swipe_from_center(300, 0, 1)
    elif sys.argv[1] == 'cmd':
        screenshot_url = f'http://localhost:{droidcast_port}'
        lastcmd = ''
        while True:
            cmd = input('------------ > ')
            if cmd == '':
                cmd = lastcmd
            if cmd == 'candeploy':
                print(can_deploy_troops())
            if cmd == 'rh':
                print(find_return_home_pos())
            if cmd == 'corner':
                swipe_from_center(500, 1, -1)
            if cmd == '2':
                print(return_home_or_stage_2())
            if cmd == 'up':
                swipe_from_center(100, 0, 1)

            lastcmd = cmd

    elif sys.argv[1] == 'droidcast':
        threading.Thread(target=start_droidcast).start()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
        while not droidcast_started:
            time.sleep(3)
        landscape_resolution()
        while True:
            time.sleep(1)
