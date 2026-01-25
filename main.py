from fastapi import FastAPI
from pydantic import BaseModel
import chess
import chess.pgn
import chess.engine
import io
from typing import Optional, List, Dict, Any

app = FastAPI(title="Stockfish PGN Analyzer", version="1.0.0")

class AnalyzeRequest(BaseModel):
    pgn: str
    initial_fen: Optional[str] = None
    depth: int = 12
    multipv: int = 2
    # Time per ply (seconds). If you prefer depth-only, keep this low.
    time_sec: float = 0.05

class MovePV(BaseModel):
    rank: int
    uci: str
    san: str
    eval_cp: Optional[int] = None

def _score_to_cp(score: chess.engine.PovScore) -> Optional[int]:
    # centipawns from the side-to-move perspective; mate scores become large cp
    s = score.pov(chess.WHITE)
    if s.is_mate():
        # represent mate as a big number with sign
        m = s.mate()
        if m is None:
            return None
        return 100000 if m > 0 else -100000
    return s.score()

def ok(*, legal: bool = True, per_ply=None, key_moments=None, status: str = "ok", error=None) -> Dict[str, Any]:
    return {
        "status": status,                 # "ok" | "partial" | "error"
        "legal": bool(legal),
        "per_ply": per_ply or [],
        "key_moments": key_moments or [],
        "error": error                    # None or {"code":..., "message":..., "details":...}
    }

def fail(code: str, message: str, details=None, *, legal: bool = False, per_ply=None, key_moments=None) -> Dict[str, Any]:
    return ok(
        legal=legal,
        per_ply=per_ply,
        key_moments=key_moments,
        status="error",
        error={
            "code": code,
            "message": message,
            "details": details or {}
        }
    )
    
@app.post("/analyze_pgn")
def analyze_pgn(req: AnalyzeRequest) -> Dict[str, Any]:
    # Parse PGN
    game = chess.pgn.read_game(io.SringIO(req.pgn))
    if game is None:
        return fail ("INVALID_PGN", "Could not parse PGN.")

    # Start board
    if req.initial_fen:
        board = chess.Board(req.initial_fen)
    else:
        board = game.board()

    # Launch Stockfish
    engine = chess.engine.SimpleEngine.popen_uci("/usr/games/stockfish")
    try:
        # configure multipv if supported
        try:
            engine.configure({"MultiPV": max(1, min(req.multipv, 3))})
        except Exception:
            pass

        per_ply: List[Dict[str, Any]] = []
        prev_eval = None

        ply = 0
        for move in game.mainline_moves():
            ply += 1

            # Legality check BEFORE pushing
            if move not in board.legal_moves:
                return fail(
                    "ILLEGAL_MOVE",
                    "Move is not legal from reconstructed position.",
                    details={
                        "first_illegal_move": {
                            "ply": ply,
                            "uci": move.uci(),
                            "fen_before": board.fen(),
                        }
                    }
                )

            san = board.san(move)
            board.push(move)

            # Analyse after the move (position for side to move)
            limit = chess.engine.Limit(depth=req.depth, time=req.time_sec)
            analysis = engine.analyse(board, limit, multipv=max(1, min(req.multipv, 3)))

            # analysis can be dict (multipv=1) or list (multipv>1)
            lines = analysis if isinstance(analysis, list) else [analysis]

            pvs: List[Dict[str, Any]] = []
            eval_cp_main = None

            for item in lines:
                pv = item.get("pv", [])
                if not pv:
                    continue
                best_uci = pv[0].uci()
                # SAN needs a board at current position
                best_san = board.san(pv[0])
                eval_cp = _score_to_cp(item["score"])
                pvs.append({
                    "rank": int(item.get("multipv", 1)),
                    "uci": best_uci,
                    "san": best_san,
                    "eval_cp": eval_cp
                })
                if item.get("multipv", 1) == 1:
                    eval_cp_main = eval_cp

            delta = None
            if prev_eval is not None and eval_cp_main is not None:
                delta = eval_cp_main - prev_eval
            prev_eval = eval_cp_main if eval_cp_main is not None else prev_eval

            per_ply.append({
                "ply": ply,
                "played_uci": move.uci(),
                "played_san": san,
                "fen": board.fen(),
                "eval_cp": eval_cp_main,
                "delta_cp": delta,
                "multipv": sorted(pvs, key=lambda x: x["rank"])
            })

        # Key moments: biggest eval swings by absolute delta
        key = [x for x in per_ply if isinstance(x.get("delta_cp"), int)]
        key_sorted = sorted(key, key=lambda x: abs(x["delta_cp"]), reverse=True)[:5]

        return ok(legal=True, per_ply=per_ply, key_moments=key_sorted)

    finally:
        engine.quit()
