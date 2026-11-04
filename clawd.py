"""Clawd, the Claude Code mascot, living on the speaker's screen while nothing is running.

Clawd belongs to Anthropic: it is Anthropic's mascot for Claude Code, and this project claims no
ownership of it. It is drawn here from the block-character art and pose tables that Claude Code itself
ships (eyes, arms, feet and the turn-around frames), so it matches the one in the terminal. The MIT
license of this project covers its own code only, not Clawd.

How Clawd decides what to do
----------------------------
Clawd has four needs that drift over time: energy, play, curiosity and focus. Whenever it finishes
something it scores every behaviour:

    score = base weight x how much the behaviour's need is felt x an energy check
            x novelty (things done recently score low) x a random nudge

and does the best one. Energy drains with active things and recovers with naps and coffee, and it sinks
at night, so evenings get sleepier. Random events break in too: rain (out comes the umbrella), shooting
stars at night (Clawd makes a wish) and fireflies after dark. Each behaviour is a small script with its
own random choices, so the same behaviour plays out differently every time.

The speaker only takes small uploads, so the monitor asks for a 2-frame clip (`Clawd.clip`) every few
seconds and lets the simulation run on in between.
"""
import math
import random

from PIL import Image, ImageDraw, ImageOps

W, H = 160, 128
BG = (11, 15, 20)
NIGHT_BG = (6, 8, 16)
P = 4  # Clawd's block width; blocks are P wide and 2P tall, like terminal quadrant blocks
S = 2  # pixel size for props
GROUND = H - 6

# ---------------------------------------------------------------- Clawd, from Claude Code's pose tables

# Quadrant block characters as the corners they fill: a top-left, b top-right, c bottom-left, d bottom-right.
QUAD = {" ": "", "▘": "a", "▝": "b", "▖": "c", "▗": "d", "▀": "ab", "▄": "cd", "▌": "ac", "▐": "bd",
        "▛": "abc", "▜": "abd", "▙": "acd", "▟": "bcd", "█": "abcd", "▚": "ad", "▞": "bc"}
CLAWD = (215, 119, 87)  # clawd_body
HOLE = (0, 0, 0)  # clawd_background: shows through the eyes and draws the eyelids
ARMS = {"down": (" ▐", "", "▝▜", "█▀"), "up": ("▗▟", "▄", " ▜", "█▘"), "one-up": (" ▐", "▄", "▝▜", "█▘")}
EYES = {"open": "▛███▛█", "left": "▟███▟█", "right": "█▟███▟", "closed": "_███_█", "wink": "▛███_█"}  # _ = lid
FEET = {"both": " ▝▝   ▝▝ ", "left": " ▝▝      ", "right": "      ▝▝ "}
TURN = {
    "right-12": (" ▐█▜██▛█ ", "▝▜██████▀", " ▝▝   ▝▝ "),
    "right-30": ("  █▛██▛▌ ", " ▝█████▛ ", "  ▘▘  ▘▘ "),
    "right-55": ("  ▐█▛█▜  ", "  ▐████  ", "  ▝▝ ▝▝  "),
    "right-75": ("   ██▛▌  ", "   ███▌  ", "   ▘  ▘  "),
    "edge": ("   ▐██   ", "   ▐██   ", "   ▝ ▝   "),
    "back-105": ("   ███▌  ", "   ███▌  ", "   ▘  ▘  "),
    "back-125": ("  ▐████  ", "  ▐████  ", "  ▝▝ ▝▝  "),
    "back-150": ("  █████▌ ", " ▝█████▛ ", "  ▘▘  ▘▘ "),
    "back": (" ▐██████ ", "▝▜██████▀", " ▝▝   ▝▝ "),
    "left-75": ("   ▛██▌  ", "   ███▌  ", "   ▘  ▘  "),
    "left-55": ("  ▐▜▛██  ", "  ▐████  ", "  ▝▝ ▝▝  "),
    "left-30": ("  ▛██▛█▌ ", " ▝█████▛ ", "  ▘▘  ▘▘ "),
    "left-12": (" ▐▛███▜█ ", "▝▜██████▀", " ▝▝   ▝▝ "),
}
# A full spin: round the right side to the back, then back to the front from the left (~ = mirrored).
SPIN = ["right-12", "right-30", "right-55", "right-75", "edge", "back-105", "back-125", "back-150", "back",
        "~back-150", "~back-125", "~back-105", "~edge", "left-75", "left-55", "left-30", "left-12"]


def clawd_rows(eyes="open", arms="down", feet="both"):
    r1l, r1r, r2l, r2r = ARMS[arms]
    return [r1l + EYES[eyes] + r1r, r2l + "█████" + r2r, FEET[feet]]


def clawd_sprite(rows, mirror=False):
    """Draw block-character rows as pixels. A character is 2 x 2 blocks, each P wide and 2P tall."""
    img = Image.new("RGBA", (9 * 2 * P, 3 * 4 * P), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for ri, row in enumerate(rows):
        for ci, ch in enumerate(row):
            x, y = ci * 2 * P, ri * 4 * P
            if ch == "_":  # closed eye: a body-coloured cell with a thin lid line along the bottom
                d.rectangle([x, y, x + 2 * P - 1, y + 4 * P - 1], fill=CLAWD)
                d.rectangle([x, y + 3 * P, x + 2 * P - 1, y + 4 * P - 1], fill=HOLE)
                continue
            for q in QUAD.get(ch, ""):
                qx, qy = x + (P if q in "bd" else 0), y + (2 * P if q in "cd" else 0)
                d.rectangle([qx, qy, qx + P - 1, qy + 2 * P - 1], fill=CLAWD)
    return ImageOps.mirror(img) if mirror else img


# ---------------------------------------------------------------- props

def mix(a, b, k):
    k = max(0.0, min(1.0, k))
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b))


def blit(d, x, y, art, colors, s=S):
    for r, row in enumerate(art):
        for c, ch in enumerate(row):
            if ch in colors:
                d.rectangle([x + c * s, y + r * s, x + c * s + s - 1, y + r * s + s - 1], fill=colors[ch])


def draw_z(d, x, y, size, color):
    blit(d, x, y, ["#####", "...#.", "..#..", ".#...", "#####"], {"#": color}, size)


def snap(v, q=P):
    return int(round(v / q)) * q


LAPTOP = ["gggggggggggggggg",
          "gGGGGGGGGGGGGGGg",
          "gGGGGGGGGGGGGGGg",
          "gGGGGGGoGGGGGGGg",
          "gGGGGGGGGGGGGGGg",
          "gGGGGGGGGGGGGGGg",
          "gggggggggggggggg"]
LAPTOP_BASE = "kkkkkkkkkkkkkkkkkk"
MUG = ["WCCW.", "WWWW#", "WWWW#", "WWWW."]
BULB = [".yyy.", "yyyyy", "yyyyy", ".yyy.", ".ggg.", "..g.."]
CHECK = ["....#", "...#.", "#.#..", ".#..."]
NOTE = ["..##", "..#.", "..#.", "###.", "##.."]
STAR = [".#.", "###", ".#."]
MOON = ["..YYY.", ".YY...", "YY....", "YY....", ".YY...", "..YYY."]
BALL = [[".WWW.", "WWRWW", "WRRRW", "WWRWW", ".WWW."],
        [".WRW.", "WWRWW", "RRWRR", "WWRWW", ".WRW."]]
UMBRELLA = ["........UUUUUUUU........",
            ".....UUUUUUUUUUUUUU.....",
            "...UUUUUUUUUUUUUUUUUU...",
            ".UUUUUUUUUUUUUUUUUUUUUU.",
            "UUUUUUUUUUUUUUUUUUUUUUUU",
            "U.U..U..U..U..U..U..U.UU"]
CODE_COLORS = [(125, 211, 252), (251, 191, 36), (74, 222, 128), (244, 114, 182), (200, 210, 225)]
NOTE_COLORS = [(196, 181, 253), (125, 211, 252), (251, 207, 232)]

# need it answers, energy cost (negative restores), base weight, when it can happen
BEHAVIOURS = {
    "stroll": ("curious", 0.04, 1.0, "any"),
    "wave": ("play", 0.02, 0.5, "any"),
    "hop_spin": ("play", 0.10, 0.7, "any"),
    "dizzy": ("play", 0.12, 0.35, "any"),
    "dance": ("play", 0.15, 0.6, "any"),
    "firefly": ("curious", 0.08, 0.9, "night"),
    "ball": ("play", 0.15, 0.6, "day"),
    "coding": ("focus", 0.08, 0.8, "any"),
    "idea": ("focus", 0.03, 0.6, "any"),
    "coffee": ("energy", -0.25, 0.5, "day"),
    "stargaze": ("curious", 0.0, 1.0, "night"),
    "peekaboo": ("play", 0.05, 0.45, "any"),
    "nap": ("energy", -0.6, 0.5, "any"),
}


def is_night(hour):
    return hour >= 20 or hour < 6


class Clawd:
    def __init__(self, seed=None, night=False, force=None, rain=False, pace=1.0):
        r = self.rng = random.Random(seed)
        self.night, self.force, self.pace = night, force, pace
        self.t = 0.0
        self.x = snap(r.uniform(8, W - 80))
        self.eyes, self.arms, self.feet, self.lift, self.turn, self.foot = "open", "down", "both", 0, None, 0
        self.need = {"energy": 0.45 if night else 0.85, "play": r.random(), "curious": r.random(),
                     "focus": r.random()}
        self.last = {}
        self.props = {}  # things that belong to the current behaviour
        self.particles = []  # z's, notes, steam, bubble pops: [kind, x, y, age, extra]
        self.blink_at, self.blink_end, self.wink = r.uniform(1, 3), -1.0, False
        self.raining = rain
        self.rain_until = 1e9 if rain else 0.0
        self.drops = [[r.uniform(0, W), r.uniform(-H, H)] for _ in range(34)]
        self.splashes = []
        self.stars = [(r.randrange(0, W), r.randrange(0, 72), r.uniform(0, 6.3), r.uniform(0.5, 2.0),
                       r.random() < 0.25) for _ in range(34)]
        self.meteor = None
        self.next_meteor = r.uniform(4, 12)
        self.next_rain = r.uniform(60, 200)
        self.interrupt, self.current = None, ""
        self.gen = self.life("umbrella" if rain and force == "umbrella" else None)
        next(self.gen)

    # ------------------------------------------------ choosing what to do

    def choose(self):
        if self.force:
            return self.force
        e, best, pick = self.need["energy"], -1.0, "stroll"
        for name, (need, cost, base, when) in BEHAVIOURS.items():
            if (when == "night" and not self.night) or (when == "day" and self.night):
                continue
            w = base * (1.0 if need == "energy" else 0.25 + self.need[need])
            if cost > 0.08:
                w *= e ** 1.5
            if name == "nap":
                w *= max(0.05, 1.2 - e) * 2 * (1.6 if self.night else 1.0)
            if name == "coffee":
                w *= max(0.05, 1.0 - e) * 1.5
            w *= 1 - 0.85 * math.exp(-(self.t - self.last.get(name, -1e9)) / 45)  # novelty
            w *= self.rng.lognormvariate(0, 0.35)
            if w > best:
                best, pick = w, name
        return pick

    def life(self, first=None):
        yield
        name = first
        while True:
            name = name or self.choose()
            self.current = name
            self.last[name] = self.t
            need, cost, _, _ = BEHAVIOURS.get(name, ("play", 0.0, 0, "any"))
            self.need["energy"] = max(0.0, min(1.0, self.need["energy"] - cost))
            yield from getattr(self, "b_" + name)()
            if need != "energy":
                self.need[need] = 0.1
            self.reset()
            yield from self.idle(self.rng.uniform(0.6, 2.4))
            name = None

    def reset(self):
        self.eyes, self.arms, self.feet, self.lift, self.turn = "open", "down", "both", 0, None
        self.props = {}

    def sleep(self):
        """Settle down for the night: nap from now on."""
        if self.force != "nap":
            self.force, self.interrupt = "nap", "nap"

    # ------------------------------------------------ small building blocks

    def wait(self, seconds):
        t = 0.0
        while t < seconds:
            t += yield

    def idle(self, seconds):
        t = 0.0
        while t < seconds:
            t += yield
            if self.rng.random() < 0.01:
                self.eyes = self.rng.choice(["left", "right", "open", "open"])

    def walk_to(self, x, speed=1.0):
        x = snap(x)
        acc = 0.0
        while self.x != x:
            acc += (yield) * speed * self.pace
            self.eyes = "right" if x > self.x else "left"
            while acc >= 0.16 and self.x != x:
                acc -= 0.16
                self.foot ^= 1
                self.x += P if x > self.x else -P
            self.feet = ("left", "right")[self.foot]
        self.feet, self.eyes = "both", "open"

    def hop(self, height=12, length=0.5, arms="up"):
        self.arms, t = arms, 0.0
        while t < length:
            t += yield
            self.lift = snap(height * math.sin(math.pi * min(1.0, t / length)))
        self.lift, self.arms = 0, "down"

    def spin(self, frame=0.09):
        t = 0.0
        while t < len(SPIN) * frame:
            t += yield
            self.turn = SPIN[min(len(SPIN) - 1, int(t / frame))]
        self.turn = None

    def look_at_x(self, x):
        mid = self.x + 36
        self.eyes = "left" if x < mid - 12 else "right" if x > mid + 12 else "open"

    def top(self):
        return GROUND - 10 * P - self.lift

    def somewhere(self, near=None, far=20):
        while True:
            x = snap(self.rng.uniform(4, W - 76))
            if near is None or abs(x - near) >= far:
                return x

    # ------------------------------------------------ behaviours

    def b_stroll(self):
        for _ in range(self.rng.randint(1, 3)):
            yield from self.walk_to(self.somewhere(self.x, 24))
            t, until = 0.0, self.rng.uniform(1.0, 3.0)
            while t < until:
                t += yield
                if self.rng.random() < 0.04:
                    self.eyes = self.rng.choice(["left", "right", "open"])
            self.eyes = "open"

    def b_wave(self):
        if self.rng.random() < 0.5:
            yield from self.walk_to(self.somewhere(self.x, 16))
        t, until = 0.0, self.rng.uniform(1.6, 3.0)
        while t < until:
            t += yield
            self.arms = "one-up" if int(t / 0.22) % 2 == 0 else "down"
        self.arms = "down"
        self.blink_at, self.wink = self.t, True

    def b_hop_spin(self):
        for _ in range(self.rng.randint(1, 3)):
            yield from self.hop(self.rng.choice([8, 12, 16]))
            yield from self.wait(0.15)
        yield from self.spin()
        if self.rng.random() < 0.5:
            yield from self.hop(8)

    def b_dizzy(self):
        for _ in range(self.rng.randint(2, 3)):
            yield from self.spin(0.06)
        t, until, base = 0.0, self.rng.uniform(2.5, 4.0), self.x
        self.props["dizzy"] = 0.0
        while t < until:
            dt = yield
            t += dt
            self.props["dizzy"] += dt * 5
            self.x = base + (P if int(t / 0.3) % 2 else 0)
            self.eyes = ("left", "right")[int(t / 0.18) % 2]  # eyes rolling
        self.x = base
        self.props = {}

    def b_dance(self):
        moves = [("up", "left", 4), ("down", "right", 0), ("one-up", "left", 4), ("down", "right", 0)]
        if self.rng.random() < 0.5:
            moves = [("up", "both", 8), ("up", "both", 0), ("down", "left", 0), ("down", "right", 0)]
        beat, t, until, n = self.rng.uniform(0.26, 0.36), 0.0, self.rng.uniform(5, 9), -1
        base = self.x
        while t < until:
            t += yield
            k = int(t / beat)
            if k != n:
                n = k
                self.arms, self.feet, self.lift = moves[k % len(moves)]
                if k % 8 in (4, 5):  # a little side step now and then
                    self.x = base + (P if k % 16 < 8 else -P)
                if k % 2 == 0:
                    self.particles.append(["note", self.x + self.rng.choice([-8, 70]), self.top() + 4, 0.0,
                                           self.rng.choice(NOTE_COLORS)])
        self.x = base

    def b_firefly(self):
        r = self.rng
        side = r.choice([-10, W + 10])
        fly = self.props["fly"] = dict(x=side, y=r.uniform(20, 60), vx=0.0, vy=0.0, tx=W / 2, ty=40,
                                       retarget=0.0, lit=True, caught=False)
        t, tries = 0.0, 0
        while t < 16 and tries < 4:
            dt = yield
            t += dt
            self.look_at_x(fly["x"])
            mid = self.x + 36
            if abs(fly["x"] - mid) > 30 and r.random() < 0.6:
                step_to = self.x + (P if fly["x"] > mid else -P) * 3
                yield from self.walk_to(max(0, min(W - 72, step_to)), 1.4)
            elif fly["y"] > self.top() - 30 and abs(fly["x"] - mid) < 26:
                tries += 1
                yield from self.hop(16, 0.45)
                if r.random() < 0.45:  # caught it!
                    fly["caught"] = True
                    self.arms, self.eyes = "up", "closed"
                    yield from self.wait(1.0)
                    self.arms, self.eyes = "down", "open"
                    fly.update(caught=False, x=mid, y=self.top() - 6, vy=-30.0, tx=r.choice([-30, W + 30]),
                               ty=-20, retarget=99)
                    self.blink_at, self.wink = self.t, True
                    yield from self.wait(2.0)
                    break
                fly.update(vy=-60.0, vx=r.choice([-70.0, 70.0]))  # it darts away
        fly.update(tx=r.choice([-40, W + 40]), ty=-30, retarget=99)
        yield from self.wait(1.5)

    def b_ball(self):
        r = self.rng
        from_left = r.random() < 0.5
        ball = self.props["ball"] = dict(x=-8.0 if from_left else W + 8.0, y=GROUND - 5.0,
                                         vx=70.0 if from_left else -70.0, vy=0.0, roll=0.0, walls=True)
        self.look_at_x(ball["x"])
        yield from self.wait(0.6)
        kicks = r.randint(2, 4)
        for k in range(kicks):
            while abs(ball["vx"]) > 8 or ball["y"] < GROUND - 6:  # watch it settle
                yield
                self.look_at_x(ball["x"])
            toward_centre = 1 if ball["x"] < W / 2 else -1
            stand = ball["x"] - 6 - 72 if toward_centre > 0 else ball["x"] + 6
            yield from self.walk_to(max(-8, min(W - 64, stand)), 1.3)
            self.look_at_x(ball["x"])
            yield from self.wait(0.2)
            self.arms = "one-up"
            last = k == kicks - 1
            ball.update(vx=toward_centre * r.uniform(60, 110) * (1.6 if last else 1), vy=-r.uniform(40, 90),
                        walls=not last)
            yield from self.hop(6, 0.3, arms="one-up")
        t = 0.0
        while t < 2.5:
            t += yield
            self.look_at_x(ball["x"])

    def b_coding(self):
        r = self.rng
        lines = [[(r.randint(2, 6), r.choice(CODE_COLORS)) for _ in range(r.randint(1, 3))]
                 for _ in range(r.randint(3, 4))]
        self.props["laptop"] = True
        self.props["code"] = dict(lines=lines, typed=0, done=False)
        total = sum(n + 1 for line in lines for n, _ in line)
        t = 0.0
        while self.props["code"]["typed"] < total:
            dt = yield
            t += dt
            self.arms = "one-up" if int(t / 0.14) % 2 else "down"
            if r.random() < dt * 9:
                self.props["code"]["typed"] += 1
            if r.random() < dt * 0.4:  # a little pause to think
                self.arms = "down"
                self.eyes = r.choice(["left", "right"])
                yield from self.wait(r.uniform(0.4, 1.0))
                self.eyes = "open"
        self.arms = "down"
        self.props["code"]["done"] = True
        yield from self.wait(0.6)
        yield from self.hop(10)
        yield from self.wait(0.8)

    def b_idea(self):
        r = self.rng
        self.props["bubble"] = 0.0
        t, until = 0.0, r.uniform(2.5, 4.5)
        while t < until:
            dt = yield
            t += dt
            self.props["bubble"] = t
            self.eyes = ("left", "open", "right", "open")[int(t / 0.7) % 4]
        self.eyes = "open"
        self.props = {"bulb": 0.0}
        t = 0.0
        while t < 1.4:
            dt = yield
            t += dt
            self.props["bulb"] = t
        yield from self.hop(14)
        self.props = {}
        yield from self.walk_to(self.somewhere(self.x, 30), 1.6)  # off to do it

    def b_coffee(self):
        r = self.rng
        self.props["mug"] = True
        self.arms = "one-up"
        t, until, next_sip = 0.0, r.uniform(6, 10), r.uniform(1.0, 2.0)
        while t < until:
            dt = yield
            t += dt
            if r.random() < dt * 3:
                self.particles.append(["steam", self.x + 72, self.top() - 2, 0.0, r.uniform(0, 6)])
            if t >= next_sip:
                self.eyes = "closed"
                yield from self.wait(r.uniform(0.6, 1.0))
                self.eyes = "open"
                next_sip = t + r.uniform(1.5, 3.0)
        self.arms = "down"
        self.props = {}

    def b_umbrella(self):
        self.props["umbrella"] = True
        self.arms = "one-up"
        while self.raining:
            if self.rng.random() < 0.5:
                yield from self.walk_to(self.somewhere(self.x, 16), 0.7)
                self.arms = "one-up"
            yield from self.wait(self.rng.uniform(1.0, 3.0))
            if self.force == "umbrella" and self.rng.random() < 0.3:
                break
        self.props = {}
        self.arms = "down"
        base = self.x
        for i in range(6):  # shake off the rain
            self.x = base + (P if i % 2 == 0 else -P)
            yield from self.wait(0.08)
        self.x = base

    def b_stargaze(self):
        r = self.rng
        t, until = 0.0, r.uniform(8, 14)
        self.next_meteor = min(self.next_meteor, self.t + r.uniform(1.5, 4))
        while t < until:
            dt = yield
            t += dt
            if self.meteor:
                self.look_at_x(self.meteor["x"])
                if self.meteor["age"] > 0.35 and not self.meteor.get("wished"):
                    self.meteor["wished"] = True
                    yield from self.wish()
            elif r.random() < dt * 0.4:
                self.eyes = r.choice(["left", "right"])  # Clawd's side glance reads as looking up
        self.eyes = "open"

    def b_wish(self):
        while self.meteor and self.meteor["age"] < 0.3:
            yield
            self.look_at_x(self.meteor["x"])
        yield from self.wish()

    def wish(self):
        yield from self.wait(0.3)
        self.eyes = "closed"
        self.arms = "up"
        yield from self.wait(1.2)
        self.arms, self.eyes = "down", "open"
        yield from self.hop(10)
        self.blink_at, self.wink = self.t, True

    def b_peekaboo(self):
        r = self.rng
        go_left = self.x + 36 < W / 2
        yield from self.walk_to(-80 if go_left else W + 8, 1.2)
        yield from self.wait(r.uniform(1.0, 2.5))
        for _ in range(r.randint(1, 3)):
            left = r.random() < 0.5
            self.x = -48 if left else W - 24  # only a sliver shows
            self.eyes = "right" if left else "left"
            yield from self.wait(r.uniform(1.0, 1.8))
            self.x = -80 if left else W + 8
            yield from self.wait(r.uniform(0.6, 1.4))
        left = r.random() < 0.5
        self.x = -76 if left else W + 4
        yield from self.walk_to(self.somewhere(), 1.2)
        yield from self.hop(10)

    def b_nap(self):
        r = self.rng
        self.eyes = "closed"
        t, until = 0.0, r.uniform(9, 15)
        next_z = 0.5
        while t < until:
            dt = yield
            t += dt
            if t >= next_z:
                if r.random() < 0.45:
                    self.props["bubble_snore"] = 0.0
                else:
                    self.particles.append(["z", self.x + 62, self.top() - 2, 0.0, None])
                next_z = t + r.uniform(1.4, 2.4)
            if "bubble_snore" in self.props:
                self.props["bubble_snore"] += dt
                if self.props["bubble_snore"] > 1.6:  # pop
                    for a in range(6):
                        self.particles.append(["pop", self.x + 80, self.top() + 8, 0.0, a * math.pi / 3])
                    del self.props["bubble_snore"]
        if self.force == "nap":  # asleep for the night: no waking up and stretching
            return
        self.eyes = "open"
        self.blink_at = self.t
        yield from self.wait(0.4)
        self.arms = "up"  # stretch
        yield from self.wait(1.0)
        self.arms = "down"

    # ------------------------------------------------ the world

    def advance(self, seconds, dt=0.05):
        for _ in range(max(1, round(seconds / dt))):
            self.step(dt)

    def step(self, dt):
        r = self.rng
        self.t += dt
        for k, rate in (("play", 0.012), ("curious", 0.014), ("focus", 0.009)):
            self.need[k] = min(1.0, self.need[k] + rate * dt)
        target = 0.3 if self.night else 0.75
        self.need["energy"] += (target - self.need["energy"]) * 0.004 * dt

        if not self.force:  # random events
            if not self.raining and not self.night and self.t >= self.next_rain:
                self.raining, self.rain_until = True, self.t + r.uniform(12, 22)
                self.interrupt = "umbrella"
            if self.raining and self.t >= self.rain_until:
                self.raining = False
                self.next_rain = self.t + r.uniform(120, 300)
        if self.night and self.meteor is None and self.t >= self.next_meteor:
            self.meteor = dict(x=r.uniform(20, W - 10), y=r.uniform(4, 20), vx=r.choice([-1, 1]) * r.uniform(60, 90),
                               vy=r.uniform(20, 35), age=0.0, trail=[])
            busy = self.current in ("nap", "coding", "stargaze", "umbrella")
            if not busy and not self.force and r.random() < 0.6:
                self.interrupt = "wish"
        if self.meteor:
            mt = self.meteor
            mt["trail"].append((mt["x"], mt["y"]))
            mt["trail"] = mt["trail"][-9:]
            mt["x"] += mt["vx"] * dt
            mt["y"] += mt["vy"] * dt
            mt["age"] += dt
            if mt["age"] > 1.6:
                self.meteor = None
                self.next_meteor = self.t + r.uniform(25, 60) / (3 if self.current == "stargaze" else 1)

        if self.interrupt:
            name, self.interrupt = self.interrupt, None
            self.reset()
            self.gen = self.life(name)
            next(self.gen)
        self.gen.send(dt)

        if self.t >= self.blink_at:
            self.blink_end = self.t + 0.15
            self.blink_at = self.t + (0.35 if r.random() < 0.15 else 1.0 + r.expovariate(1 / 3.0))
            if not self.wink:
                self.wink = r.random() < 0.08
        if self.t > self.blink_end + 0.05:
            self.wink = False

        fly = self.props.get("fly")
        if fly and not fly["caught"]:
            fly["retarget"] -= dt
            if fly["retarget"] <= 0:  # wanders, often teasing close to Clawd
                near = r.random() < 0.5
                fly["tx"] = self.x + 36 + r.uniform(-30, 30) if near else r.uniform(10, W - 10)
                fly["ty"] = r.uniform(self.top() - 30, self.top() - 4) if near else r.uniform(10, 70)
                fly["retarget"] = r.uniform(0.6, 1.8)
            fly["vx"] += ((fly["tx"] - fly["x"]) * 2.0 - fly["vx"] * 1.6 + r.gauss(0, 40)) * dt
            fly["vy"] += ((fly["ty"] - fly["y"]) * 2.0 - fly["vy"] * 1.6 + r.gauss(0, 40)) * dt
            fly["x"] += fly["vx"] * dt
            fly["y"] += fly["vy"] * dt
            if r.random() < dt * 1.5:
                fly["lit"] = not fly["lit"]
        ball = self.props.get("ball")
        if ball:
            ball["vy"] += 260 * dt
            ball["x"] += ball["vx"] * dt
            ball["y"] += ball["vy"] * dt
            ball["roll"] += ball["vx"] * dt / 6
            if ball["y"] > GROUND - 5:
                ball["y"] = GROUND - 5
                ball["vy"] = -ball["vy"] * 0.55 if abs(ball["vy"]) > 25 else 0.0
                ball["vx"] *= 0.985 ** (dt * 60)
            if ball["walls"] and (ball["x"] < 6 or ball["x"] > W - 6):
                ball["x"] = max(6, min(W - 6, ball["x"]))
                ball["vx"] = -ball["vx"] * 0.7

        for p in self.particles:
            p[3] += dt
            if p[0] == "note":
                p[1] += 6 * math.sin(p[3] * 4) * dt
                p[2] -= 14 * dt
            elif p[0] == "steam":
                p[1] += 5 * math.sin(p[3] * 3 + p[4]) * dt
                p[2] -= 10 * dt
            elif p[0] == "z":
                p[1] += 4 * dt
                p[2] -= 6 * dt
            elif p[0] == "pop":
                p[1] += 14 * math.cos(p[4]) * dt
                p[2] += 14 * math.sin(p[4]) * dt
        life = {"note": 2.5, "steam": 1.4, "z": 3.4, "pop": 0.35}
        self.particles = [p for p in self.particles if p[3] < life[p[0]]]

        if self.raining:
            for drop in self.drops:
                drop[1] += 130 * dt
                drop[0] -= 18 * dt
                if drop[1] > GROUND:
                    self.splashes.append([drop[0], 0.0])
                    drop[0], drop[1] = r.uniform(0, W + 20), r.uniform(-30, -4)
        for s in self.splashes:
            s[1] += dt
        self.splashes = [s for s in self.splashes if s[1] < 0.2]

    # ------------------------------------------------ drawing

    def draw(self, blinks=True, x=None):
        img = Image.new("RGB", (W, H), NIGHT_BG if self.night else BG)
        d = ImageDraw.Draw(img)
        cx = self.x if x is None else x
        top = GROUND - 10 * P - self.lift
        if self.night:
            for sx, sy, ph, sp, big in self.stars:
                b = 0.35 + 0.65 * max(0.0, math.sin(self.t * sp + ph))
                d.rectangle([sx, sy, sx + (1 if big else 0), sy + (1 if big else 0)],
                            fill=mix(NIGHT_BG, (230, 235, 255), b))
            blit(d, W - 24, 6, MOON, {"Y": (250, 236, 180)})
        if self.meteor:
            trail = self.meteor["trail"] + [(self.meteor["x"], self.meteor["y"])]
            for i, (mx, my) in enumerate(trail):
                d.rectangle([mx, my, mx + 1, my + 1], fill=mix(NIGHT_BG, (255, 250, 220), (i + 1) / len(trail)))
        if self.raining:
            covered = (cx + 2, cx + 74, top - 26) if self.props.get("umbrella") else None
            for x0, y0 in self.drops:
                if covered and covered[0] <= x0 <= covered[1] and y0 > covered[2]:
                    continue
                d.line([(x0, y0), (x0 + 1, y0 - 5)], fill=(110, 140, 190))
            for sx, _ in self.splashes:
                d.point([(sx - 2, GROUND - 1), (sx + 2, GROUND - 1)], fill=(140, 170, 210))

        if self.turn:
            rows, mirror = TURN[self.turn.lstrip("~")], self.turn.startswith("~")
        else:
            eyes = self.eyes
            if blinks and self.t < self.blink_end and eyes != "closed":
                eyes = "wink" if self.wink else "closed"
            rows, mirror = clawd_rows(eyes, self.arms, self.feet), False
        sprite = clawd_sprite(rows, mirror)
        img.paste(sprite, (round(cx), round(top)), sprite)

        pr = self.props
        if pr.get("laptop"):
            lx = cx + 36 - 16
            blit(d, lx, GROUND - 18, LAPTOP, {"g": (120, 128, 140), "G": (160, 168, 180), "o": (230, 236, 244)})
            blit(d, lx - 2, GROUND - 4, [LAPTOP_BASE], {"k": (90, 96, 108)})
        code = pr.get("code")
        if code:
            bx, by = max(2, min(W - 56, cx + 10)), top - 32
            d.rectangle([bx, by, bx + 53, by + 25], fill=(18, 24, 34), outline=(90, 104, 126))
            left = code["typed"]
            for i, line in enumerate(code["lines"]):
                px = bx + 4
                for n, col in line:
                    k = min(n, max(0, left))
                    if k:
                        d.rectangle([px, by + 4 + i * 5, px + k * 2 - 1, by + 5 + i * 5], fill=col)
                    left -= n + 1
                    px += (n + 1) * 2
            if code["done"]:
                blit(d, bx + 40, by + 15, CHECK, {"#": (74, 222, 128)})
            elif int(self.t * 3) % 2:
                d.rectangle([bx + 46, by + 19, bx + 48, by + 21], fill=(230, 236, 244))
        if "bubble" in pr:
            bx, by = cx + 46, top - 30
            d.ellipse([cx + 40, top - 4, cx + 44, top], fill=(220, 226, 236))
            d.ellipse([cx + 44, top - 12, cx + 50, top - 6], fill=(220, 226, 236))
            d.ellipse([bx, by, bx + 30, by + 18], fill=(220, 226, 236))
            for k in range(1 + int(pr["bubble"] / 0.5) % 3):
                d.rectangle([bx + 8 + k * 6, by + 8, bx + 9 + k * 6, by + 9], fill=(60, 70, 90))
        if "bulb" in pr:
            bx, by = cx + 31, top - 22
            blit(d, bx, by, BULB, {"y": (255, 226, 110), "g": (150, 156, 170)})
            if int(pr["bulb"] * 6) % 2:
                for ddx, ddy in ((-6, 2), (14, 2), (4, -6), (-4, -4), (12, -4)):
                    d.rectangle([bx + ddx, by + ddy, bx + ddx + 1, by + ddy + 1], fill=(255, 240, 160))
        if pr.get("mug"):
            blit(d, cx + 70, top + 2, MUG, {"W": (236, 236, 240), "C": (120, 72, 40), "#": (236, 236, 240)})
        if pr.get("umbrella"):
            d.rectangle([cx + 67, top - 9, cx + 68, top + 8], fill=(140, 120, 100))
            for r_, row in enumerate(UMBRELLA):
                for c_, ch in enumerate(row):
                    if ch == "U":
                        col = (230, 236, 244) if (c_ // 4) % 2 else (96, 140, 230)
                        d.rectangle([cx + 2 + c_ * 3, top - 26 + r_ * 3, cx + 4 + c_ * 3, top - 24 + r_ * 3],
                                    fill=col)
        if "dizzy" in pr:
            for k in range(3):
                a = pr["dizzy"] + k * 2 * math.pi / 3
                blit(d, cx + 33 + 24 * math.cos(a), top - 8 + 4 * math.sin(a), STAR, {"#": (255, 224, 102)})
        if "bubble_snore" in pr:
            rad = 2 + pr["bubble_snore"] * 5
            bx, by = cx + 80, top + 8
            d.ellipse([bx - rad, by - rad, bx + rad, by + rad], outline=(150, 200, 240))
            d.point([(bx - rad / 2, by - rad / 2)], fill=(230, 245, 255))
        ball = pr.get("ball")
        if ball:
            blit(d, ball["x"] - 5, ball["y"] - 5, BALL[int(abs(ball["roll"])) % 2],
                 {"W": (240, 240, 244), "R": (230, 80, 80)})
        fly = pr.get("fly")
        if fly and not fly["caught"]:
            fx, fy = round(fly["x"]), round(fly["y"])
            if fly["lit"]:
                d.rectangle([fx - 4, fy - 1, fx + 5, fy + 2], fill=(46, 62, 18))
                d.rectangle([fx - 1, fy - 4, fx + 2, fy + 5], fill=(46, 62, 18))
                d.rectangle([fx - 2, fy - 2, fx + 3, fy + 3], fill=(120, 160, 40))
                d.rectangle([fx - 1, fy - 1, fx + 2, fy + 2], fill=(236, 255, 140))
            else:
                d.rectangle([fx, fy, fx + 1, fy + 1], fill=(90, 100, 50))
        bg = NIGHT_BG if self.night else BG
        for kind, px, py, age, extra in self.particles:
            if kind == "note":
                blit(d, px, py, NOTE, {"#": mix(extra, bg, age / 2.5)})
            elif kind == "steam":
                d.rectangle([px, py, px + 1, py + 1], fill=mix((200, 200, 210), bg, age / 1.4))
            elif kind == "z":
                draw_z(d, round(px / 2) * 2, round(py / 2) * 2, 2 if age < 1.7 else 3,
                       mix((210, 220, 235), bg, age / 3.4))
            elif kind == "pop":
                d.rectangle([px, py, px + 1, py + 1], fill=(170, 210, 245))
        return img

    def clip(self, sim=0.4):
        """Two frames for the speaker: now, and a moment later with Clawd held in place. 500 ms each."""
        a = self.draw(blinks=False)
        x = self.x
        self.advance(sim)
        return [a, self.draw(blinks=False, x=x)], 500
