export function apiPath(path: string): string {
  const clean = path.startsWith("/") ? path : `/${path}`;
  return `/api/leads-acquisition${clean}`;
}
