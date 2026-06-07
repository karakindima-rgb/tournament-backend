from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime, inspect as sa_inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, relationship
from pydantic import BaseModel
from datetime import datetime, timezone
import os, json

DB_URL = os.getenv("DATABASE_URL", "sqlite:///tournament.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase): pass

class Tournament(Base):
    __tablename__ = "tournaments"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    started = Column(Boolean, default=False)
    finished = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    players = relationship("Player", back_populates="tournament", cascade="all,delete")
    games = relationship("Game", back_populates="tournament", cascade="all,delete")

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    tournament_id = Column(Integer, ForeignKey("tournaments.id"))
    tournament = relationship("Tournament", back_populates="players")

class Game(Base):
    __tablename__ = "games"
    id = Column(Integer, primary_key=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id"))
    round = Column(Integer)
    circle = Column(Integer, default=1)
    team_a = Column(String)   # JSON: "[1,2]"
    team_b = Column(String)   # JSON: "[3,4]"
    resting = Column(String, default="[]")  # JSON: "[5]" or "[]"
    score_a = Column(Integer, nullable=True)
    score_b = Column(Integer, nullable=True)
    tournament = relationship("Tournament", back_populates="games")

# Migrate
with engine.connect() as conn:
    insp = sa_inspect(engine)
    if insp.has_table("games"):
        game_cols = {c["name"] for c in insp.get_columns("games")}
        if "player_a" in game_cols:
            # Old 1v1 schema — drop everything
            conn.execute(text("DROP TABLE IF EXISTS games"))
            conn.execute(text("DROP TABLE IF EXISTS players"))
            conn.execute(text("DROP TABLE IF EXISTS tournaments"))
            conn.commit()
    if insp.has_table("tournaments"):
        t_cols = {c["name"] for c in insp.get_columns("tournaments")}
        if "finished" not in t_cols:
            conn.execute(text("ALTER TABLE tournaments ADD COLUMN finished BOOLEAN DEFAULT 0"))
            conn.commit()

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Tournament API")
_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def db():
    s = SessionLocal()
    try: yield s
    finally: s.close()

def get_t(tid: int, s: Session):
    t = s.get(Tournament, tid)
    if not t: raise HTTPException(404, "Tournament not found")
    return t

def game_dict(g: Game) -> dict:
    return {
        "id": g.id,
        "round": g.round,
        "circle": g.circle or 1,
        "team_a": json.loads(g.team_a),
        "team_b": json.loads(g.team_b),
        "resting": json.loads(g.resting or "[]"),
        "score_a": g.score_a,
        "score_b": g.score_b,
    }

# ── 2v2 schedule ────────────────────────────────────────────────────────────────

def generate_schedule(players: list) -> list:
    n = len(players)
    if n < 4:
        return []
    rounds = []

    if n == 4:
        # 3 rounds, no resting, all 3 possible splits
        splits = [(0,1,2,3), (0,2,1,3), (0,3,1,2)]
        for a0,a1,b0,b1 in splits:
            rounds.append({
                "team_a": [players[a0], players[a1]],
                "team_b": [players[b0], players[b1]],
                "resting": [],
            })

    elif n == 5:
        # Perfect schedule: each pair is teammates exactly once
        # Indices into players list: (rest, a0,a1, b0,b1)
        configs = [
            (4, 0,2, 1,3),
            (3, 0,1, 2,4),
            (2, 0,3, 1,4),
            (1, 0,4, 2,3),
            (0, 1,2, 3,4),
        ]
        for ri, a0,a1,b0,b1 in configs:
            rounds.append({
                "team_a": [players[a0], players[a1]],
                "team_b": [players[b0], players[b1]],
                "resting": [players[ri]],
            })

    elif n % 2 == 1:
        # Odd n >= 7: n rounds, 1 player + (n-5) players rest per round
        # Use circle rotation, 4 active players play each round
        lst = players[:]
        for _ in range(n):
            rest_main = lst[0]
            active = lst[1:5]          # next 4 play
            extra_rest = lst[5:]       # rest of the bench
            rounds.append({
                "team_a": [active[0], active[3]],
                "team_b": [active[1], active[2]],
                "resting": [rest_main] + list(extra_rest),
            })
            lst = [lst[0]] + [lst[-1]] + lst[1:-1]

    else:
        # Even n >= 6: n-1 rounds, 2+ players rest per round
        lst = players[:]
        for _ in range(n - 1):
            active = lst[:4]
            resting = lst[4:]
            rounds.append({
                "team_a": [active[0], active[3]],
                "team_b": [active[1], active[2]],
                "resting": list(resting),
            })
            lst = [lst[0]] + [lst[-1]] + lst[1:-1]

    return rounds

# ── API ─────────────────────────────────────────────────────────────────────────

class TCreate(BaseModel):
    name: str

class PCreate(BaseModel):
    name: str

class ScoreIn(BaseModel):
    score_a: int
    score_b: int

@app.get("/api/health")
def health(): return {"status": "ok"}

def t_dict(t: Tournament) -> dict:
    return {"id": t.id, "name": t.name, "started": t.started, "finished": bool(t.finished), "created_at": t.created_at}

@app.post("/api/tournaments")
def create_t(body: TCreate, s: Session = Depends(db)):
    t = Tournament(name=body.name)
    s.add(t); s.commit(); s.refresh(t)
    return t_dict(t)

@app.get("/api/tournaments")
def list_t(s: Session = Depends(db)):
    return [t_dict(t) for t in s.query(Tournament).all()]

@app.get("/api/tournaments/{tid}")
def get_tournament(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    return {**t_dict(t), "players": [{"id": p.id, "name": p.name} for p in t.players]}

@app.post("/api/tournaments/{tid}/finish")
def finish_t(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    t.finished = True
    s.commit()
    return t_dict(t)

@app.post("/api/tournaments/{tid}/players")
def add_player(tid: int, body: PCreate, s: Session = Depends(db)):
    t = get_t(tid, s)
    if t.started: raise HTTPException(400, "Already started")
    p = Player(name=body.name, tournament_id=tid)
    s.add(p); s.commit(); s.refresh(p)
    return {"id": p.id, "name": p.name}

@app.post("/api/tournaments/{tid}/start")
def start_t(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    if t.started: raise HTTPException(400, "Already started")
    if len(t.players) < 4: raise HTTPException(400, "Need at least 4 players")
    pids = [p.id for p in t.players]
    schedule = generate_schedule(pids)
    games = []
    for r, rd in enumerate(schedule):
        g = Game(
            tournament_id=tid, round=r+1, circle=1,
            team_a=json.dumps(rd["team_a"]),
            team_b=json.dumps(rd["team_b"]),
            resting=json.dumps(rd["resting"]),
        )
        s.add(g); games.append(g)
    t.started = True
    s.commit()
    return [game_dict(g) for g in games]

@app.post("/api/tournaments/{tid}/add_circle")
def add_circle(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    if not t.started: raise HTTPException(400, "Tournament not started")
    existing = s.query(Game).filter(Game.tournament_id == tid).all()
    if any(g.score_a is None for g in existing):
        raise HTTPException(400, "Current circle not finished")
    max_round = max((g.round for g in existing), default=0)
    max_circle = max((g.circle or 1 for g in existing), default=1)
    pids = [p.id for p in t.players]
    schedule = generate_schedule(pids)
    games = []
    for r, rd in enumerate(schedule):
        g = Game(
            tournament_id=tid, round=max_round+r+1, circle=max_circle+1,
            team_a=json.dumps(rd["team_a"]),
            team_b=json.dumps(rd["team_b"]),
            resting=json.dumps(rd["resting"]),
        )
        s.add(g); games.append(g)
    s.commit()
    return [game_dict(g) for g in games]

@app.get("/api/tournaments/{tid}/games")
def get_games(tid: int, s: Session = Depends(db)):
    get_t(tid, s)
    games = s.query(Game).filter(Game.tournament_id == tid).all()
    return [game_dict(g) for g in games]

@app.put("/api/games/{gid}/score")
def set_score(gid: int, body: ScoreIn, s: Session = Depends(db)):
    g = s.get(Game, gid)
    if not g: raise HTTPException(404)
    g.score_a = body.score_a
    g.score_b = body.score_b
    s.commit()
    return game_dict(g)

@app.get("/api/tournaments/{tid}/standings")
def standings(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    all_games = s.query(Game).filter(Game.tournament_id == tid).all()
    # Only count circles where every game is scored
    from collections import defaultdict
    by_circle = defaultdict(list)
    for g in all_games:
        by_circle[g.circle or 1].append(g)
    complete = {c for c, gs in by_circle.items() if all(g.score_a is not None for g in gs)}
    stats = {p.id: {"player_id": p.id, "name": p.name, "wins": 0, "losses": 0, "games_played": 0, "diff": 0}
             for p in t.players}
    for g in all_games:
        if (g.circle or 1) not in complete:
            continue
        team_a = json.loads(g.team_a)
        team_b = json.loads(g.team_b)
        a_win = g.score_a > g.score_b
        for pid in team_a:
            if pid not in stats: continue
            stats[pid]["games_played"] += 1
            stats[pid]["diff"] += g.score_a - g.score_b
            if a_win: stats[pid]["wins"] += 1
            else:     stats[pid]["losses"] += 1
        for pid in team_b:
            if pid not in stats: continue
            stats[pid]["games_played"] += 1
            stats[pid]["diff"] += g.score_b - g.score_a
            if not a_win: stats[pid]["wins"] += 1
            else:         stats[pid]["losses"] += 1
    return sorted(stats.values(), key=lambda x: (-x["wins"], -x["diff"]))
