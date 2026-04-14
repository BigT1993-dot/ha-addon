# Changelog

All notable changes to this add-on will be documented in this file.

The format is based on Keep a Changelog, as recommended by the Home Assistant developer documentation.

## [Unreleased]

## [0.3.16] - 2026-04-14

### Changed

- Reset restore timers after the add-on switches to `minpv` and wait for `evcc` to confirm `minpv` before evaluating a restore to `pv`.
- Stop switching back to `pv` on grid import; sustained import now only clears `auto_mode_active` and hands control back to `evcc`.

## [0.3.15] - 2026-04-14

### Changed

- Lower the default battery discharge restore threshold to `100 W` for `60 s`.
- Clear `auto_mode_active` once `evcc` takes over current regulation above the configured `offeredCurrent` threshold.

## [0.3.13] - 2026-04-09

### Added

- Add percent-based sensor search for SoC fields.
- Show percent sensor list errors in the UI.

## [0.3.12] - 2026-04-09

### Changed

- Use Europe/Berlin timestamps for runtime history.
