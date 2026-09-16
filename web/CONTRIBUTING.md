# Working in `web/`

## Commands

```bash
npm ci            # install exactly what the lockfile says
npm run dev       # dev server
npm run typecheck # tsc --noEmit
npm run lint      # eslint
npm run test      # vitest, single run
npm run test:watch
npm run test:contract  # golden fixtures shared with the Python agent
```

## Known trap: `npm install <pkg>` breaks vitest

Vitest 5 uses rolldown, whose platform-specific native binding ships as an
*optional* dependency. npm has a long-standing bug
([npm/cli#4828](https://github.com/npm/cli/issues/4828)) where installing or
uninstalling any package drops optional dependencies that were already there.
The next `npm run test` then fails with:

```
Error: Cannot find native binding.
  cause: Cannot find module '@rolldown/binding-wasm32-wasi'
```

Nothing is actually wrong with your change. The fix is a clean reinstall:

```bash
rm -rf node_modules package-lock.json && npm install
```

Confirm the binding came back — the directory should hold a
`binding-<platform>` entry, not just `pluginutils`:

```bash
ls node_modules/@rolldown/
```

This bites on any `npm install <pkg>` or `npm uninstall <pkg>`, so budget the
reinstall whenever you add a dependency. `npm ci` is unaffected, which is why
CI does not hit this.

## `@types/node` is pinned ahead of the Node runtime

`@types/node` is `^22` while CI runs Node 20. Vitest 5 requires the newer
types; the types package does not have to match the runtime version. Do not
"fix" this by downgrading — the install will fail its peer check.
