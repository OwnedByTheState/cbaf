from cbaf import *
from random import randint as r
mt = Minitouch()
while True:
    mt.send(f'd 0 100 100 50', commit=True)
    mt.send('u 0', commit=True)

