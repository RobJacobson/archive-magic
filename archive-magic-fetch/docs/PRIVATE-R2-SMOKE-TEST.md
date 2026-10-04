# Optional private S3/R2 lifecycle smoke check

This is an opt-in live procedure for a disposable bucket/prefix. The automated
suite uses simulated buckets and never needs real cloud writes. The local user
workspace migration also performs no bucket operations.

1. Copy examples/collections/example.org/collection.toml into a fresh collection
   directory outside your code checkout. Configure a disposable bucket/prefix,
   credentials through the AWS chain, and a small acquisition date range.
2. Keep storage.local.directory separate from authored inputs. Use the annotated
   workspace structure in the root README. Add name/homepage and optional assets
   if testing presentation publication.
3. Run Fetch. Check that data/ contains WARCs and replay indexes and discovery/
   contains source/query/version-qualified completed caches. No logs or settings
   should appear in the bucket.
4. Run --publish-metadata. Confirm assets precede the generated archive.json.
5. Start Navigator with a catalog listing this bucket. Confirm authenticated WARC
   range replay. Its cache/navigator/ contains indexes/assets, never full WARCs.
6. Interrupt an upload and retry --sync-only. Completed local data/caches survive;
   the retry must not contact capture sources or delete other bucket content.
7. Run --evict-local. Content verification must succeed before local output is
   removed. Collection definitions/assets remain, and Navigator continues serving.
8. A normal Fetch run now must require --restore. Run explicit --restore, verify
   local replay indexes and cache provenance, then resume Fetch without losing
   older captures. Clear Navigator's cache and confirm a fresh bucket-only start.
9. On disposable data only, test --reset-data: no date overrides, explicit warning,
   managed data/ deletion, and preservation of discovery/, metadata, assets, and
   unrelated objects. Remote reset uses rclone and requires its installation.

Do not run these destructive or chargeable live steps against a production
collection merely to validate a local workspace migration.
