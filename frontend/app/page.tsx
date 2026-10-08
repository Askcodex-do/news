import { headers } from "next/headers";

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
  // Forward the visitor's IP to the backend, which resolves it to a country and
  // serves the matching local edition (spec §5, §21). On failure the backend
  // returns the global edition; the page is never blocked.
  const forwardedFor = headers().get("x-forwarded-for") ?? undefined;
  const feed = await getFeed(undefined, forwardedFor);

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
