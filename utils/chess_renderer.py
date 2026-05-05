from PIL import Image, ImageDraw, ImageFont
import io
import os
import chess
import chess.svg

try:
    import cairosvg
    _CAIRO_OK = True
except ImportError:
    _CAIRO_OK = False

# ── Dimensions ────────────────────────────────────────────────────────────────
SQ          = 64            # pixels per square
BOARD_PX    = SQ * 8       # 512
MARGIN      = 22            # left + bottom for rank/file labels
IMG_SIZE    = BOARD_PX + MARGIN  # 534

# ── Board colors ──────────────────────────────────────────────────────────────
LIGHT_SQ  = (240, 217, 181)
DARK_SQ   = (181, 136,  99)
BG        = ( 49,  46,  43)
LABEL_FG  = (200, 190, 175)

# ── Highlight tints (RGBA) ────────────────────────────────────────────────────
TINT_LAST     = (220, 210,  60, 120)   # last move squares
TINT_SELECTED = (100, 200,  80, 130)   # selected piece
TINT_CHECK    = (220,  60,  60, 140)   # king in check

# ── Move indicator ────────────────────────────────────────────────────────────
DOT_COLOR   = ( 20,  20,  20, 110)
DOT_RING_W  = 4

# ── Fallback Unicode glyphs (used only if cairosvg is unavailable) ─────────────
PIECE_SYM = {
    (chess.KING,   chess.WHITE): "♔",
    (chess.QUEEN,  chess.WHITE): "♕",
    (chess.ROOK,   chess.WHITE): "♖",
    (chess.BISHOP, chess.WHITE): "♗",
    (chess.KNIGHT, chess.WHITE): "♘",
    (chess.PAWN,   chess.WHITE): "♙",
    (chess.KING,   chess.BLACK): "♚",
    (chess.QUEEN,  chess.BLACK): "♛",
    (chess.ROOK,   chess.BLACK): "♜",
    (chess.BISHOP, chess.BLACK): "♝",
    (chess.KNIGHT, chess.BLACK): "♞",
    (chess.PAWN,   chess.BLACK): "♟",
}

PIECE_FONT_SIZE = 47

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_piece_img_cache: dict[tuple, Image.Image] = {}


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _font_cache:
        return _font_cache[size]
    for path in _FONT_PATHS:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[size] = font
                return font
            except Exception:
                pass
    font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _get_piece_image(piece: chess.Piece) -> Image.Image:
    """Return a SQ×SQ RGBA PIL image for the given piece using chess.svg."""
    key = (piece.piece_type, piece.color)
    if key in _piece_img_cache:
        return _piece_img_cache[key]

    svg_str = chess.svg.piece(piece, size=SQ)
    png_bytes = cairosvg.svg2png(bytestring=svg_str.encode(), output_width=SQ, output_height=SQ)
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    _piece_img_cache[key] = img
    return img


def _sq_xy(sq: chess.Square, perspective: chess.Color) -> tuple[int, int]:
    """Return top-left (x, y) pixel of a square, respecting board perspective."""
    file = chess.square_file(sq)
    rank = chess.square_rank(sq)
    if perspective == chess.WHITE:
        col, row = file, 7 - rank
    else:
        col, row = 7 - file, rank
    return MARGIN + col * SQ, row * SQ


def _tint(draw: ImageDraw.ImageDraw, sq: chess.Square,
          perspective: chess.Color, color: tuple) -> None:
    x, y = _sq_xy(sq, perspective)
    draw.rectangle([x, y, x + SQ - 1, y + SQ - 1], fill=color)


def _move_dots(draw: ImageDraw.ImageDraw, board: chess.Board,
               targets: set, perspective: chess.Color) -> None:
    for sq in targets:
        x, y = _sq_xy(sq, perspective)
        cx, cy = x + SQ // 2, y + SQ // 2
        if board.piece_at(sq) is not None:
            r = SQ // 2 - 4
            draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                         outline=DOT_COLOR, width=DOT_RING_W)
        else:
            r = SQ // 8
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DOT_COLOR)


def _draw_piece_glyph(draw: ImageDraw.ImageDraw, piece: chess.Piece,
                      x: int, y: int) -> None:
    """Fallback: draw piece as a Unicode glyph (used when cairosvg is missing)."""
    glyph = PIECE_SYM[(piece.piece_type, piece.color)]
    font  = _get_font(PIECE_FONT_SIZE)
    fg      = (255, 255, 255) if piece.color == chess.WHITE else ( 20,  20,  20)
    outline = ( 30,  30,  30) if piece.color == chess.WHITE else (230, 230, 230)

    bbox = draw.textbbox((0, 0), glyph, font=font)
    gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    gx = x + (SQ - gw) // 2 - bbox[0]
    gy = y + (SQ - gh) // 2 - bbox[1] - 2

    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((gx + dx, gy + dy), glyph, font=font, fill=outline)
    draw.text((gx, gy), glyph, font=font, fill=fg)


def _draw_labels(draw: ImageDraw.ImageDraw, perspective: chess.Color) -> None:
    font = _get_font(12)
    for col in range(8):
        char = chr(ord("a") + col) if perspective == chess.WHITE else chr(ord("h") - col)
        x = MARGIN + col * SQ + SQ // 2
        bb = draw.textbbox((0, 0), char, font=font)
        draw.text((x - (bb[2] - bb[0]) // 2, BOARD_PX + 4), char, font=font, fill=LABEL_FG)

    for row in range(8):
        char = str(8 - row) if perspective == chess.WHITE else str(row + 1)
        y = row * SQ + SQ // 2
        bb = draw.textbbox((0, 0), char, font=font)
        draw.text((3, y - (bb[3] - bb[1]) // 2), char, font=font, fill=LABEL_FG)


def render_board(
    board: chess.Board,
    *,
    selected: "chess.Square | None" = None,
    move_targets: "set | None" = None,
    last_move: "chess.Move | None" = None,
    perspective: chess.Color = chess.WHITE,
) -> io.BytesIO:
    """
    Render the board to a PNG BytesIO.

    selected     – square of the currently selected piece (green tint)
    move_targets – legal destination squares (move dots)
    last_move    – previous move (yellow tint on from/to squares)
    perspective  – chess.WHITE = rank 1 at bottom; chess.BLACK = rank 8 at bottom
    """
    move_targets = move_targets or set()

    # Base board squares
    img  = Image.new("RGB", (IMG_SIZE, IMG_SIZE), BG)
    draw = ImageDraw.Draw(img)

    for sq in chess.SQUARES:
        file, rank = chess.square_file(sq), chess.square_rank(sq)
        color = LIGHT_SQ if (file + rank) % 2 == 1 else DARK_SQ
        x, y  = _sq_xy(sq, perspective)
        draw.rectangle([x, y, x + SQ - 1, y + SQ - 1], fill=color)

    # RGBA overlay for tints and dots
    overlay  = Image.new("RGBA", (IMG_SIZE, IMG_SIZE), (0, 0, 0, 0))
    ov_draw  = ImageDraw.Draw(overlay)

    if last_move:
        _tint(ov_draw, last_move.from_square, perspective, TINT_LAST)
        _tint(ov_draw, last_move.to_square,   perspective, TINT_LAST)
    if selected is not None:
        _tint(ov_draw, selected, perspective, TINT_SELECTED)
    if board.is_check():
        king_sq = board.king(board.turn)
        if king_sq is not None:
            _tint(ov_draw, king_sq, perspective, TINT_CHECK)
    if move_targets:
        _move_dots(ov_draw, board, move_targets, perspective)

    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    # Pieces
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece:
            x, y = _sq_xy(sq, perspective)
            if _CAIRO_OK:
                piece_img = _get_piece_image(piece)
                img.paste(piece_img, (x, y), piece_img)
            else:
                draw = ImageDraw.Draw(img)
                _draw_piece_glyph(draw, piece, x, y)

    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Labels
    _draw_labels(draw, perspective)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
