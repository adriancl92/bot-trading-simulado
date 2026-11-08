# Bot de trading simulado (paper trading)

Bot que se ejecuta solo cada ~15 minutos con GitHub Actions, lee **precios reales de BTC/EUR en Kraken** y simula cinco carteras en paralelo (momentum, tendencia, cruce de medias, reversión RSI y comprar y mantener), cada una con el mismo capital. No mueve dinero real.

- **Panel:** la web de GitHub Pages de este repositorio.
- **Reiniciar con otro capital:** pestaña *Actions* → *Bot de trading (simulado)* → *Run workflow* → escribe el capital.
- **Ajustes** (comisión, stop-loss, parámetros, mercado): `config.json`. Si cambias el mercado o la vela, la simulación se reinicia.

No es asesoramiento financiero.
