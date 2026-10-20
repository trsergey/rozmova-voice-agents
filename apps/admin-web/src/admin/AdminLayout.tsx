// The admin shell: navigation, the user menu and, for viewers, the read-only banner.
import { DropdownMenu } from "radix-ui";
import { NavLink, Outlet } from "react-router";

import { useCanEdit, useCurrentSession, useLogout } from "./session";

const NAV = [
  { to: "/admin/bots", label: "Боти" },
  { to: "/admin/calls", label: "Дзвінки" },
  { to: "/admin/kb", label: "База знань" },
  { to: "/admin/ops", label: "Операції" },
  { to: "/admin/access-codes", label: "Коди доступу", adminOnly: true },
  { to: "/admin/demo", label: "Демо-чат" },
];

export function AdminLayout() {
  const canEdit = useCanEdit();
  const link = "rounded-md px-3 py-2 text-sm whitespace-nowrap";
  const idle = "text-fg-muted hover:bg-muted-bg hover:text-fg";

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-line bg-surface px-4 py-3 md:px-6">
        <p className="font-semibold whitespace-nowrap">
          Rozmova <span className="font-normal text-fg-muted">адмінка</span>
        </p>
        <UserMenu />
      </header>
      {!canEdit && (
        <p role="status" className="bg-warn-bg px-4 py-2 text-sm text-warn-fg md:px-6">
          Режим перегляду: ви бачите все, але змінювати нічого не можна.
        </p>
      )}
      <div className="flex flex-1 flex-col md:flex-row">
        <nav
          aria-label="Розділи"
          className="flex gap-1 overflow-x-auto border-b border-line px-2 py-2 md:w-52 md:shrink-0 md:flex-col md:border-r md:border-b-0 md:py-4"
        >
          {NAV.filter((item) => canEdit || !item.adminOnly).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `${link} ${isActive ? "bg-muted-bg font-medium text-fg" : idle}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <main className="min-w-0 flex-1 px-4 py-6 md:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function UserMenu() {
  const session = useCurrentSession();
  const logout = useLogout();
  const role = session.user.role === "admin" ? "адміністратор" : "перегляд";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger aria-label={`${session.user.name}, меню`} className="flex items-center gap-2 rounded-md px-2 py-1 text-sm hover:bg-muted-bg">
        <span className="hidden max-w-64 truncate sm:inline">{session.user.name}</span>
        <span className="rounded bg-muted-bg px-1.5 py-0.5 text-xs text-fg-muted">{role}</span>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="min-w-40 rounded-md border border-line bg-surface p-1 text-sm text-fg shadow-lg"
        >
          <DropdownMenu.Item
            className="cursor-pointer rounded px-2 py-1.5 outline-none data-[highlighted]:bg-muted-bg"
            onSelect={() => logout.mutate()}
          >
            Вийти
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
