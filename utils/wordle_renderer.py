from PIL import Image, ImageDraw, ImageFont
import io

# Colors (Wordle dark theme)
BG         = (18, 18, 19)
EMPTY_BG   = (18, 18, 19)
EMPTY_BD   = (58, 58, 60)
FILLED_BD  = (86, 87, 88)
GREEN      = (83, 141, 78)
YELLOW     = (181, 159, 59)
GRAY       = (58, 58, 60)
KEY_DEF    = (129, 131, 132)
KEY_DARK   = (58, 58, 60)
WHITE      = (255, 255, 255)
BLACK      = (18, 18, 19)

TILE_SIZE  = 58
TILE_GAP   = 5
PADDING    = 16

KEY_W      = 34
KEY_H      = 46
KEY_GAP    = 4
KB_TOP     = 16

KEYBOARD_ROWS = ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"]

# Base image width on the widest keyboard row (10 keys)
_MAX_KB_W  = len(KEYBOARD_ROWS[0]) * KEY_W + (len(KEYBOARD_ROWS[0]) - 1) * KEY_GAP
IMG_W      = _MAX_KB_W + PADDING * 2

BOARD_W    = TILE_SIZE * 5 + TILE_GAP * 4
BOARD_X    = (IMG_W - BOARD_W) // 2  # center board horizontally

BOARD_H    = PADDING * 2 + TILE_SIZE * 6 + TILE_GAP * 5
KB_H       = 3 * KEY_H + 2 * KEY_GAP + KB_TOP + PADDING
IMG_H      = BOARD_H + KB_H


def _get_font(size):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        try:
            return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
        except Exception:
            return ImageFont.load_default()


def _tile_color(state):
    if state == "green":  return GREEN
    if state == "yellow": return YELLOW
    if state == "gray":   return GRAY
    return None


def _draw_rounded_rect(draw, xy, radius, fill, outline=None, width=2):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill, outline=outline, width=width)


def _draw_board(draw, guesses, feedbacks, font_large):
    for row in range(6):
        for col in range(5):
            x = PADDING + col * (TILE_SIZE + TILE_GAP)
            y = PADDING + row * (TILE_SIZE + TILE_GAP)
            x1, y1 = x + TILE_SIZE, y + TILE_SIZE

            if row < len(feedbacks):
                fb_tokens = [c for c in feedbacks[row] if c in ("🟩", "🟨", "⬛")]
                fb_token = fb_tokens[col] if col < len(fb_tokens) else "⬛"
                state = (
                    "green"  if fb_token == "🟩" else
                    "yellow" if fb_token == "🟨" else
                    "gray"
                )
                color = _tile_color(state)
                _draw_rounded_rect(draw, (x, y, x1, y1), 4, fill=color)
                letter = guesses[row][col].upper()
                bbox = draw.textbbox((0, 0), letter, font=font_large)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.text((x + (TILE_SIZE - tw) // 2, y + (TILE_SIZE - th) // 2 - 2), letter, font=font_large, fill=WHITE)
            elif row == len(guesses) and row < 6:
                _draw_rounded_rect(draw, (x, y, x1, y1), 4, fill=EMPTY_BG, outline=FILLED_BD, width=2)
            else:
                _draw_rounded_rect(draw, (x, y, x1, y1), 4, fill=EMPTY_BG, outline=EMPTY_BD, width=2)


def _draw_keyboard(draw, letter_states, font_small, y_offset):
    for ri, row in enumerate(KEYBOARD_ROWS):
        row_width = len(row) * KEY_W + (len(row) - 1) * KEY_GAP
        x_start = (IMG_W - row_width) // 2
        y = y_offset + ri * (KEY_H + KEY_GAP)
        for ci, letter in enumerate(row):
            x = x_start + ci * (KEY_W + KEY_GAP)
            state = letter_states.get(letter.lower())
            if state == "green":  key_color = GREEN
            elif state == "yellow": key_color = YELLOW
            elif state == "gray":   key_color = KEY_DARK
            else:                   key_color = KEY_DEF
            _draw_rounded_rect(draw, (x, y, x + KEY_W, y + KEY_H), 4, fill=key_color)
            bbox = draw.textbbox((0, 0), letter, font=font_small)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(
                (x + (KEY_W - tw) // 2, y + (KEY_H - th) // 2 - 1),
                letter, font=font_small, fill=WHITE
            )


def render_frame(guesses, feedbacks, letter_states, tile_row=-1, tile_col=-1, squish=0.0):
    """
    squish: 0.0 = full tile, 1.0 = fully squished (mid-flip)
    tile_row/tile_col: which tile is animating (-1 = none)
    """
    img = Image.new("RGB", (IMG_W, IMG_H), BG)
    draw = ImageDraw.Draw(img)
    font_large = _get_font(32)
    font_small = _get_font(16)

    for row in range(6):
        for col in range(5):
            x = BOARD_X + col * (TILE_SIZE + TILE_GAP)
            y = PADDING + row * (TILE_SIZE + TILE_GAP)
            x1, y1 = x + TILE_SIZE, y + TILE_SIZE

            if row == tile_row and col == tile_col and squish > 0:
                cy = (y + y1) // 2
                half = int((TILE_SIZE // 2) * (1 - squish))
                ty, ty1 = cy - half, cy + half
            else:
                ty, ty1 = y, y1

            if row < len(feedbacks):
                fb_tokens = [c for c in feedbacks[row] if c in ("🟩", "🟨", "⬛")]
                fb_token = fb_tokens[col] if col < len(fb_tokens) else "⬛"
                state = (
                    "green"  if fb_token == "🟩" else
                    "yellow" if fb_token == "🟨" else
                    "gray"
                )
                if row == tile_row and col == tile_col and squish > 0.5:
                    color = EMPTY_BG
                else:
                    color = _tile_color(state)
                _draw_rounded_rect(draw, (x, ty, x1, ty1), 4, fill=color)
                if not (row == tile_row and col == tile_col and squish > 0.3):
                    letter = guesses[row][col].upper()
                    bbox = draw.textbbox((0, 0), letter, font=font_large)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    cy_text = (ty + ty1) // 2
                    draw.text((x + (TILE_SIZE - tw) // 2, cy_text - th // 2 - 1), letter, font=font_large, fill=WHITE)
            elif row == len(guesses):
                _draw_rounded_rect(draw, (x, ty, x1, ty1), 4, fill=EMPTY_BG, outline=FILLED_BD, width=2)
            else:
                _draw_rounded_rect(draw, (x, ty, x1, ty1), 4, fill=EMPTY_BG, outline=EMPTY_BD, width=2)

    kb_y = BOARD_H + KB_TOP
    _draw_keyboard(draw, letter_states, font_small, kb_y)
    return img


def build_reveal_gif(guesses, feedbacks, letter_states):
    """
    Build an animated GIF revealing the latest guess row tile by tile.
    Returns bytes.
    """
    frames = []
    durations = []

    prev_guesses = guesses[:-1]
    prev_feedbacks = feedbacks[:-1]
    cur_row = len(guesses) - 1

    # Static frame before animation (previous state)
    frames.append(render_frame(prev_guesses, prev_feedbacks, {}))
    durations.append(100)

    SQUISH_STEPS = [0.0, 0.5, 1.0, 0.5, 0.0]
    STEP_DURATION = 60

    for col in range(5):
        partial_feedbacks = list(prev_feedbacks) + [feedbacks[cur_row]]
        partial_guesses = list(prev_guesses) + [guesses[cur_row]]
        for si, squish in enumerate(SQUISH_STEPS):
            frame = render_frame(partial_guesses, partial_feedbacks, letter_states if si >= 3 else {}, tile_row=cur_row, tile_col=col, squish=squish)
            frames.append(frame)
            durations.append(STEP_DURATION)

    # Final static frame
    frames.append(render_frame(guesses, feedbacks, letter_states))
    durations.append(800)

    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True,
        append_images=frames[1:],
        duration=durations, loop=1, optimize=False
    )
    buf.seek(0)
    return buf


def build_static_image(guesses, feedbacks, letter_states):
    """Static PNG for initial board or non-animated update."""
    img = render_frame(guesses, feedbacks, letter_states)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
