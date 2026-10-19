// An admin path that matches no page.
import { Link } from "react-router";

import { PageHeader } from "./ui";

export function NotFound() {
  return (
    <>
      <PageHeader title="Сторінку не знайдено" />
      <Link to="/admin/bots" className="text-sm text-accent hover:underline">
        До списку ботів
      </Link>
    </>
  );
}
