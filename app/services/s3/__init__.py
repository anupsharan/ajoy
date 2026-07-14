"""S3 — long-only US-stock ask-wall breakout scalper (Moomoo OpenD).

Modules
-------
types            shared dataclasses / enums for the event pipeline
moomoo_client    MoomooClient — broker/data adapter (all SDK-specific code)
normalizer       MarketDataNormalizer — validation, dedupe, sequencing
bars             BarAggregator — 1-min/5-min bars, EMA9, session VWAP, HOD
book_analyzer    OrderBookAnalyzer — robust depth baseline + wall lifecycle
tape_analyzer    TapeAnalyzer — aggressor inference + order-flow metrics
strategy_engine  StrategyEngine — signal state machine (per symbol)
risk_manager     RiskManager — sizing + every pre-trade limit
order_manager    OrderManager — race-safe OMS state machine
position_manager PositionManager — tranches, R-based exits, hard stop
event_recorder   EventRecorder — JSONL capture for replay
replay_engine    ReplayEngine — feed recorded events back through the pipeline
engine           S3Engine — orchestrator thread wired into the FastAPI app
"""
