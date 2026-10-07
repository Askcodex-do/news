import type { ArticleCard } from "@/lib/api";

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diff / 60000);
  if (mins < 60) return `${Math.max(mins, 1)}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export default function StoryCard({ article }: { article: ArticleCard }) {
  return (
    <article className="card">
      <h3>{article.headline}</h3>
      {article.subtitle ? <p>{article.subtitle}</p> : null}
      <div className="meta">
        {article.category ? <span>{article.category}</span> : null}
        {article.location ? <span>{article.location}</span> : null}
        <span title="Confidence score">Confidence {Math.round(article.confidence_score)}</span>
        {article.published_at ? <span>{timeAgo(article.published_at)}</span> : null}
      </div>
    </article>
  );
}
