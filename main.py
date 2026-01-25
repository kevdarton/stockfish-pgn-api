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
    # Parse PGN safely
    try:
        game = chess.pgn.read_game(io.StringIO(req.pgn))
    except Exception as e:
        return fail("INVALID_PGN", "Could not parse PGN.", details={"exception": str(e)})

    if game is None:
        return fail("INVALID_PGN", "Could not parse PGN.")

    # Start board
    try:
        if req.initial_fen:
            board = chess.Board(req.initial_fen)
        else:
            board = game.board()
    except Exception as e:
        return fail("INVALID_FEN", "Initial FEN is invalid.", details={"exception": str(e)})

    engine = None
    try:
        # Launch Stockfish
        engine = chess.engine.SimpleEngine.popen_uci("/usr/games/stockfish")

        # configure multipv if supported
        try:
            engine.configure({"MultiPV": max(1, min(req.multipv, 3))})
        except Exception:
            pass

        per_ply: List[Dict[str, Any]] = []
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
                    },
                )

            san = board.san(move)
            board.push(move)

            # Analyse after the move (position for side to move)
            limit = chess.engine.Limit(depth=req.depth, time=req.time_sec)
            analysis = engine.analyse(
                board,
                limit,
                multipv=max(1, min(req.multipv, 3)),
            )

            lines = analysis if isinstance(analysis, list) else [analysis]

            pvs: List[Dict[str, Any]] = []
            eval_cp_main = None

            for item in lines:
                pv = item.get("pv", [])
                if not pv:
                    continue

                best_move = pv[0]
                best_uci = best_move.uci()
                best_san = board.san(best_move)
                eval_cp = _score_to_cp(item["score"])

                rank = int(item.get("multipv", 1))
                pvs.append({
                    "rank": rank,
                    "uci": best_uci,
                    "san": best_san,
                    "eval_cp": eval_cp
                })

                if rank == 1:
                    eval_cp_main = eval_cp

            per_ply.append({
                "ply": ply,
                "played_uci": move.uci(),
                "played_san": san,
                "fen_after": board.fen(),
                "eval_cp": eval_cp_main,
                "pvs": sorted(pvs, key=lambda x: x["rank"]),
            })

        # Key moments: largest eval swings (simple heuristic)
        # (You may already have a smarter key-moment selector elsewhere.)
        key_moments: List[Dict[str, Any]] = []
        prev = None
        for row in per_ply:
            cur = row.get("eval_cp")
            if prev is not None and cur is not None:
                swing = abs(cur - prev)
                key_moments.append({
                    "ply": row["ply"],
                    "played_san": row["played_san"],
                    "eval_cp": cur,
                    "swing": swing
                })
            prev = cur

        key_sorted = sorted(key_moments, key=lambda x: x.get("swing", 0), reverse=True)[:5]

        return ok(legal=True, per_ply=per_ply, key_moments=key_sorted)

    except Exception as e:
        # Always return stable envelope, never crash ASGI
        return fail("INTERNAL_ERROR", "Unexpected internal error during analysis.", details={"exception": str(e)})

    finally:
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
