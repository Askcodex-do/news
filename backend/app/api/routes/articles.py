from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.article import Article, ArticleImage, ArticleSource
from app.schemas import ArticleCard, ArticleDetail, ArticleImageOut, SourceAttribution

router = APIRouter(prefix="/articles", tags=["articles"])


@router.get("", response_model=list[ArticleCard])
async def list_articles(
    session: AsyncSession = Depends(get_session),
    limit: int = 20,
) -> list[Article]:
    limit = max(1, min(limit, 100))
    stmt = (
        select(Article)
        .where(Article.is_published.is_(True))
        .order_by(Article.published_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/{slug}", response_model=ArticleDetail)
async def get_article(slug: str, session: AsyncSession = Depends(get_session)) -> ArticleDetail:
    article = (
        await session.execute(select(Article).where(Article.slug == slug))
    ).scalar_one_or_none()
    if article is None or not article.is_published:
        raise HTTPException(status_code=404, detail="article not found")

    sources = (
        (await session.execute(select(ArticleSource).where(ArticleSource.article_id == article.id)))
        .scalars()
        .all()
    )

    detail = ArticleDetail.model_validate(article)
    detail.sources = [
        SourceAttribution(source_name=s.source_name, url=s.url, is_independent=s.is_independent)
        for s in sources
    ]
    # Only the transient reference is exposed, never stored bytes (spec §20).
    image = (
        await session.execute(
            select(ArticleImage).where(ArticleImage.article_id == article.id).limit(1)
        )
    ).scalar_one_or_none()
    detail.image = ArticleImageOut.model_validate(image) if image else None
    return detail
