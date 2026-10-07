import StoryCard from "@/components/StoryCard";
import { getFeed } from "@/lib/api";
import type { ArticleCard } from "@/lib/api";

function Column({ title, articles, empty }: { title: string; articles: ArticleCard[]; empty: string }) {
  return (
    <section>
      <h2 className="section-title">{title}</h2>
      {articles.length === 0 ? (
        <div className="empty">{empty}</div>
      ) : (
        articles.map((a) => <StoryCard key={a.id} article={a} />)
      )}
    </section>
  );
}

export default async function HomePage() {
  // Phase 1: no IP geolocation yet, so the page renders the global edition.
  // Phase 5 wires the visitor's country through to the local column.
  const feed = await getFeed();

  const globalArticles = feed?.global_articles ?? [];
  const localArticles = feed?.local_articles ?? [];
  const localTitle = feed?.local_source_name
    ? `Local — ${feed.country_code} (${feed.local_source_name})`
    : `Local — ${feed?.country_code ?? "GLOBAL"}`;

  return (
    <>
      <header className="masthead">
        <div>
          <h1>News AI</h1>
          <div className="tagline">
            Original reporting, synthesized from multiple verified sources.
          </div>
        </div>
        <span className="badge">Accuracy before speed</span>
      </header>

      <main className="container">
        <div className="grid">
          <Column
            title="Global"
            articles={globalArticles}
            empty="No verified global stories yet. The platform publishes only after multi-source verification."
          />
          <Column
            title={localTitle}
            articles={localArticles}
            empty="No local stories yet for this edition."
          />
        </div>

        <div className="notice">
          Articles on this platform are generated independently from the evidence
          reported by multiple sources. Each published article lists its sources.
          Unverified information is not published as fact.
        </div>
      </main>
    </>
  );
}
