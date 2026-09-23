<!-- azpbmd-live -->
**Live:** not a Minecraft plugin

Host-side catalog scanner. No game server loads it.
<!-- /azpbmd-live -->
# AZPBMD item catalog scanner

Read-only scan of survival playerdata and Anvil region/entity files. Builds a public catalog of non-vanilla items for dontplaythisserver.com.

Chunks and player inventories are **not** rewritten until something loads them, so the world is a mix of NBT generations (1.14 `tag` items through 26.2 `components`, pre-1.18 `Level` chunks, 1.17+ entity regions). The scanner reads whatever is on disk and canonicalizes name/lore so the same relic is one catalog card.

**Does not write world files. Does not store coordinates, player UUIDs, or dimensions in the published JSON.**

Run at idle IO / nice 19 so the Minecraft processes keep the disk.

```bash
./run-scan.sh                 # players first, then every dimension
./run-scan.sh --players-only  # inventories + ender chests only
```

Output lives in `output/catalog.json` and `output/progress.json`. With `--push` those two files are copied to the Montreal site `/var/www/azpbmd/data/`.

Resume is automatic (`output/catalog.sqlite`). SIGINT/SIGTERM finish the current file and exit.
