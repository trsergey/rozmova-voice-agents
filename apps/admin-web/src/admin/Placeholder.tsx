// A page whose ticket has not landed yet, so the navigation already leads somewhere.
import { Link } from "react-router";

import { PageHeader } from "./ui";

export function Placeholder({ title, ticket }: { title: string; ticket: string }) {
  return (
    <>
      <PageHeader title={title} />
      <p className="text-sm text-fg-muted">Сторінка з'явиться в {ticket}.</p>
    </>
  );
}

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
