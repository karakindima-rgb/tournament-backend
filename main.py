from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, relationship
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import os, itertools, json

DB_URL = os.getenv("DATABASE_URL", "sqlite:///tournament.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase): pass

class Tournament(Base):
    __tablename__ = "tournaments"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    user_id = Column(Integer, default=0)
    started = Column(Boolean, default=False)
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
    player_a = Column(Integer)
    player_b = Column(Integer)
    resting = Column(Integer, nullable=True)
    score_a = Column(Integer, nullable=True)
    score_b = Column(Integer, nullable=True)
    circle = Column(Integer, default=1)
    tournament = relationship("Tournament", back_populates="games")

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

# --- Schedule algorithm ---
def combinations2(lst):
    return list(itertools.combinations(lst, 2))

def generate_schedule(players):
    n = len(players)
    if n < 3: return []
    rounds = []
    # Round-robin using circle method
    if n % 2 == 1:
        lst = players[:]
        for i in range(n):
            rest = lst[0]
            active = lst[1:]
            games = [(active[j], active[n - 2 - j]) for j in range((n - 1) // 2)]
            rounds.append({"games": games, "resting": rest})
            lst = [lst[0]] + [lst[-1]] + lst[1:-1]
    else:
        lst = players[:]
        for i in range(n - 1):
            games = [(lst[j], lst[n - 1 - j]) for j in range(n // 2)]
            rounds.append({"games": games, "resting": None})
            lst = [lst[0]] + [lst[-1]] + lst[1:-1]
    return rounds

# --- API ---
class TCreate(BaseModel):
    name: str

class PCreate(BaseModel):
    name: str

class ScoreIn(BaseModel):
    score_a: int
    score_b: int

@app.get("/api/health")
def health(): return {"status": "ok"}

@app.post("/api/tournaments")
def create_t(body: TCreate, s: Session = Depends(db)):
    t = Tournament(name=body.name)
    s.add(t); s.commit(); s.refresh(t)
    return {"id": t.id, "name": t.name, "started": t.started, "created_at": t.created_at}

@app.get("/api/tournaments")
def list_t(s: Session = Depends(db)):
    return [{"id": t.id, "name": t.name, "started": t.started, "created_at": t.created_at} for t in s.query(Tournament).all()]

@app.get("/api/tournaments/{tid}")
def get_tournament(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    return {"id": t.id, "name": t.name, "started": t.started, "players": [{"id": p.id, "name": p.name} for p in t.players]}

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
    if len(t.players) < 3: raise HTTPException(400, "Need at least 3 players")
    pids = [p.id for p in t.players]
    schedule = generate_schedule(pids)
    games = []
    for r, rd in enumerate(schedule):
        for a, b in rd["games"]:
            g = Game(tournament_id=tid, round=r+1, player_a=a, player_b=b, resting=rd["resting"], circle=1)
            s.add(g); games.append(g)
    t.started = True
    s.commit()
    return [{"id": g.id, "round": g.round, "circle": g.circle, "player_a": g.player_a, "player_b": g.player_b, "resting": g.resting} for g in games]

@app.post("/api/tournaments/{tid}/add_circle")
def add_circle(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    if not t.started:
        raise HTTPException(400, "Tournament not started")
    existing = s.query(Game).filter(Game.tournament_id == tid).all()
    unfinished = [g for g in existing if g.score_a is None]
    if unfinished:
        raise HTTPException(400, "Current circle not finished")
    max_round = max((g.round for g in existing), default=0)
    max_circle = max((g.circle or 1 for g in existing), default=1)
    pids = [p.id for p in t.players]
    schedule = generate_schedule(pids)
    games = []
    for r, rd in enumerate(schedule):
        for a, b in rd["games"]:
            g = Game(tournament_id=tid, round=max_round+r+1, player_a=a, player_b=b, resting=rd["resting"], circle=max_circle+1)
            s.add(g); games.append(g)
    s.commit()
    return [{"id": g.id, "round": g.round, "circle": g.circle, "player_a": g.player_a, "player_b": g.player_b, "resting": g.resting} for g in games]

@app.get("/api/tournaments/{tid}/games")
def get_games(tid: int, s: Session = Depends(db)):
    get_t(tid, s)
    games = s.query(Game).filter(Game.tournament_id == tid).all()
    return [{"id": g.id, "round": g.round, "circle": g.circle or 1, "player_a": g.player_a, "player_b": g.player_b, "resting": g.resting, "score_a": g.score_a, "score_b": g.score_b} for g in games]

@app.put("/api/games/{gid}/score")
def set_score(gid: int, body: ScoreIn, s: Session = Depends(db)):
    g = s.get(Game, gid)
    if not g: raise HTTPException(404)
    g.score_a = body.score_a; g.score_b = body.score_b
    s.commit()
    return {"id": g.id, "score_a": g.score_a, "score_b": g.score_b}

@app.get("/api/tournaments/{tid}/standings")
def standings(tid: int, s: Session = Depends(db)):
    t = get_t(tid, s)
    stats = {p.id: {"player_id": p.id, "name": p.name, "wins": 0, "losses": 0, "games_played": 0, "diff": 0} for p in t.players}
    for g in s.query(Game).filter(Game.tournament_id == tid, Game.score_a != None).all():
        if g.score_a is None: continue
        stats[g.player_a]["games_played"] += 1; stats[g.player_b]["games_played"] += 1
        stats[g.player_a]["diff"] += g.score_a - g.score_b
        stats[g.player_b]["diff"] += g.score_b - g.score_a
        if g.score_a > g.score_b:
            stats[g.player_a]["wins"] += 1; stats[g.player_b]["losses"] += 1
        else:
            stats[g.player_b]["wins"] += 1; stats[g.player_a]["losses"] += 1
    return sorted(stats.values(), key=lambda x: (-x["wins"], -x["diff"]))
