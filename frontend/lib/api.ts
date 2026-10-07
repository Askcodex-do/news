/**
 * Server-side API access. The backend URL is only read on the server, so the
 * browser never learns internal service addresses or credentials.
 */

export interface ArticleCard {
  id: string;
  slug: string;
  headline: string;
  subtitle: string | null;
  category: string | null;
  location: string | null;
  confidence_score: number;
  importance_score: number;
  published_at: string | null;
}

export interface FeedResponse {
  country_code: string;
  resolved: boolean;
  global_articles: ArticleCard[];
  local_articles: ArticleCard[];
  local_source_name: string | null;
}

const BACKEND =
  process.env.BACKEND_INTERNAL_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://localhost:8000";

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${BACKEND}${path}`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function getFeed(country?: string): Promise<FeedResponse | null> {
  const query = country ? `?country=${encodeURIComponent(country)}` : "";
  return getJson<FeedResponse>(`/feed${query}`);
}
