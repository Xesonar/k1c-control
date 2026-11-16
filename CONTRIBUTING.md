# Contributing

Small, focused changes are welcome. Describe the printer model, firmware, and enabled components when reporting an issue.

## Run checks

```bash
python3 -m unittest discover -p 'test_*.py'
python3 -m unittest discover -s deploy -p 'test_*.py'
```

The core uses the Python standard library. Install FFmpeg and ffprobe to run the synthetic media integration checks; those checks use generated video rather than a real printer camera.

## Keep real installations private

Use `config.example.json` as the shape of your configuration. Keep tokens, passwords, endpoints, chat IDs, databases, uploaded models, and recordings out of commits and issue attachments. Configuration examples use generic hostnames and require an explicitly configured owner.

## Printer actions

Tests should use isolated state and fake adapters. Do not add tests that contact a real printer, send Telegram messages, move hardware, heat the printer, or start a print. Preserve fresh checks, explicit start confirmations, and the rule that uncertain physical commands are never replayed automatically.

## License

Contributions are distributed under AGPL-3.0-only. Preserve existing copyright notices and third-party attributions in `NOTICE`, `SCORING_LICENSE.txt`, and the HLS.js license files.
