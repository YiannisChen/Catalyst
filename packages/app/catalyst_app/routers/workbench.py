from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from catalyst_app.dependencies import get_workbench_store
from catalyst_app.schemas import (
    FundamentalsResponse,
    NewsItem,
    NewsResponse,
    OhlcvResponse,
    RangeLocalResponse,
    SessionResponse,
    TickersResponse,
)
from catalyst_app.workbench_store import WorkbenchStoreError


router = APIRouter(prefix="/api", tags=["workbench"])


@router.get("/tickers", response_model=TickersResponse)
def get_tickers(store=Depends(get_workbench_store)) -> TickersResponse:
    try:
        symbols = store.list_tickers()
    except WorkbenchStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return TickersResponse(symbols=symbols, count=len(symbols))


@router.get("/ohlcv/{ticker}", response_model=OhlcvResponse)
def get_ohlcv(
    ticker: str,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    store=Depends(get_workbench_store),
) -> OhlcvResponse:
    if not ticker.strip():
        raise HTTPException(status_code=400, detail="ticker must not be blank")
    try:
        candles = store.read_ohlcv(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
        )
    except WorkbenchStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return OhlcvResponse(symbol=ticker.upper(), candles=candles, count=len(candles))


@router.get("/range-local", response_model=RangeLocalResponse)
def get_range_local(store=Depends(get_workbench_store)) -> RangeLocalResponse:
    try:
        payload = store.local_range()
    except WorkbenchStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RangeLocalResponse.model_validate(payload)


@router.get("/news/{ticker}", response_model=NewsResponse)
def get_news(
    ticker: str,
    trade_date: str = Query(...),
    window_days: int = Query(default=3, ge=0, le=30),
    limit: int = Query(default=20, ge=1, le=100),
    store=Depends(get_workbench_store),
) -> NewsResponse:
    if not ticker.strip():
        raise HTTPException(status_code=400, detail="ticker must not be blank")
    try:
        items = store.list_news(
            ticker=ticker,
            trade_date=trade_date,
            window_days=window_days,
            limit=limit,
        )
    except WorkbenchStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return NewsResponse(
        ticker=ticker.upper(),
        trade_date=trade_date,
        items=[NewsItem(**item) for item in items],
        count=len(items),
    )


@router.get("/fundamentals/{ticker}", response_model=FundamentalsResponse)
def get_fundamentals(
    ticker: str,
    trade_date: str = Query(...),
    store=Depends(get_workbench_store),
) -> FundamentalsResponse:
    if not ticker.strip():
        raise HTTPException(status_code=400, detail="ticker must not be blank")
    try:
        result = store.get_fundamentals(ticker=ticker, trade_date=trade_date)
    except WorkbenchStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        return FundamentalsResponse(ticker=ticker.upper(), reference_date=None, metrics={})
    return FundamentalsResponse(
        ticker=result["ticker"],
        reference_date=result["reference_date"],
        metrics=result["metrics"],
    )

@router.get("/session/{ticker}", response_model=SessionResponse)
def get_session(
    ticker: str,
    trade_date: str = Query(...),
    store=Depends(get_workbench_store),
) -> SessionResponse:
    """Return real OHLCV session data for the selected ticker and trade date."""
    if not ticker.strip():
        raise HTTPException(status_code=400, detail="ticker must not be blank")
    try:
        result = store.get_session(ticker=ticker, trade_date=trade_date)
    except WorkbenchStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        return SessionResponse(
            ticker=ticker.upper(),
            trade_date=trade_date,
            is_trading_day=False,
        )
    return SessionResponse.model_validate(result)
