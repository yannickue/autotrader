# Broker universe inventory (Lane U, stage 1)

Selection rule `U1-all-cash-cfd-non-stock-plus-bluechip-stocks`; account currency EUR; server clock = UTC+2 (estimated from fresh ticks).
Sources: list 2026-09-30T22:59:13.093869+00:00, probe 2026-09-30T23:04:54.059696+00:00.
Read-only scan: no orders, no order_check, no login. Symbols added to Market Watch by the scan: ABBN.CH, ADAUSD, AIR.FR, ALV.GE, ASML.NE, AUDCAD, AUDCHF, AUDJPY, AUDNZD, AUDUSD, AVAXUSD, AZN.UK, BAS.GE, BCHUSD, BMW.GE, BNP.FR, BP.UK, Bra50, CADCHF, CADJPY, CHFJPY, ChinaA50, Cocoa, Coffee, CoffeeR, Cotton, DOGEUSD, DOTUSD, DTE.GE, Diesel, ENI.IT, ETHUSD, EURAUD, EURCAD, EURCHF, EURGBP, EURHUF, EURJPY, EURMXN, EURNOK, EURNZD, EURPLN, EURSEK, EURSGD, Esp35, Euro50, Fra40, GBPAUD, GBPCAD, GBPCHF, GBPJPY, GBPNZD, GLE.FR, GOLD, GerMid50, GerTec, HKInd, HSBA.UK, IBE.ES, INGA.NE, ISP.IT, ITX.ES, Ita40, Jp225, LCrude, LINKUSD, LTCUSD, MC.FR, NESN.CH, NGas, NOVN.CH, NZDCAD, NZDCHF, NZDJPY, NZDUSD, Neth25, OR.FR, Palladium, Platinum, SAN.ES, SAN.FR, SAP.GE, SGDJPY, SHELL.NE, SIE.GE, SILVER, SOLUSD, Sugar, Swi20, UK100, UNIUSD, USDBRL, USDCAD, USDCNH, USDHUF, USDMXN, USDNOK, USDPLN, USDRUB, USDSEK, USDSGD, USDZAR, UsaInd, UsaRus, XLMUSD, XRPUSD (all deselected again afterwards, Market Watch restored to the 10 originally visible symbols).

Listed 519, probed 115, SHADOW_READY 61, NOT_READY 458 (of which PENDING only because the quote was stale/absent while the market was closed at scan time: 46; re-scan during open hours).

## Counts per class

| cluster | listed | SHADOW_READY |
|---|---|---|
| BONDS | 5 | 0 |
| COMMODITY_SOFT | 5 | 0 |
| CRYPTO | 13 | 8 |
| ENERGY | 4 | 2 |
| FUTURES_DATED | 33 | 0 |
| FX | 53 | 42 |
| INDEX | 18 | 5 |
| METALS | 4 | 4 |
| STOCKS | 384 | 0 |

## ACTIVE discovery universe (production specs; not shadow-generated)

- GER40 = `Ger40`: NOT_READY quote_stale_11118s_market_closed_or_halted
- NAS100 = `UsaTec`: SHADOW_READY
- SPX500 = `Usa500`: SHADOW_READY
- XAUUSD = `GOLD`: SHADOW_READY
- EURUSD = `EURUSD`: SHADOW_READY
- BTCUSD = `BTCUSD`: SHADOW_READY
- BRENT = `Brent`: NOT_READY quote_stale_7518s_market_closed_or_halted

## SHADOW_READY (shadow universe)

| symbol | cluster | ccy | spread med/p95 (frac of price) | lev | M5 bars | margin min lot EUR |
|---|---|---|---|---|---|---|
| AUDCAD | FX | CAD | 0.01720% / 0.02833% | 20.0 | 12000 | 30.65 |
| AUDCHF | FX | CHF | 0.02585% / 0.03792% | 20.0 | 12000 | 30.65 |
| AUDJPY | FX | JPY | 0.01006% / 0.02744% | 20.0 | 12000 | 30.65 |
| AUDNZD | FX | NZD | 0.02433% / 0.03812% | 20.0 | 12000 | 30.65 |
| AUDUSD | FX | USD | 0.00864% / 0.04032% | 20.0 | 12000 | 30.65 |
| BCHUSD | CRYPTO | USD | 0.65668% / 0.75095% | 2.0 | 12000 | 1.37 |
| BTCUSD | CRYPTO | USD | 0.05652% / 0.07213% | 2.0 | 12000 | 369.61 |
| CADCHF | FX | CHF | 0.02896% / 0.04599% | 30.0 | 12000 | 20.67 |
| CADJPY | FX | JPY | 0.01537% / 0.02893% | 30.0 | 12000 | 20.67 |
| CHFJPY | FX | JPY | 0.00955% / 0.03078% | 30.0 | 12000 | 35.21 |
| ETHUSD | CRYPTO | USD | 0.14871% / 0.15354% | 2.0 | 12000 | 11.88 |
| EURAUD | FX | AUD | 0.01103% / 0.01594% | 20.0 | 12000 | 50.00 |
| EURCAD | FX | CAD | 0.01178% / 0.01364% | 30.0 | 12000 | 33.33 |
| EURCHF | FX | CHF | 0.01373% / 0.01796% | 30.0 | 12000 | 33.33 |
| EURGBP | FX | GBP | 0.01053% / 0.01873% | 30.0 | 12000 | 33.33 |
| EURHUF | FX | HUF | 0.07204% / 0.41149% | 20.0 | 12000 | 50.00 |
| EURJPY | FX | JPY | 0.00729% / 0.01458% | 30.0 | 12000 | 33.33 |
| EURMXN | FX | MXN | 0.04630% / 0.18135% | 20.0 | 12000 | 50.00 |
| EURNOK | FX | NOK | 0.02559% / 0.08391% | 20.0 | 12000 | 50.00 |
| EURNZD | FX | NZD | 0.01491% / 0.02883% | 20.0 | 12000 | 50.00 |
| EURPLN | FX | PLN | 0.03072% / 0.11438% | 20.0 | 12000 | 50.00 |
| EURSEK | FX | SEK | 0.02372% / 0.07812% | 20.0 | 12000 | 50.00 |
| EURSGD | FX | SGD | 0.04421% / 0.05042% | 20.0 | 12000 | 50.00 |
| EURUSD | FX | USD | 0.00441% / 0.00971% | 30.0 | 12000 | 33.33 |
| GBPAUD | FX | AUD | 0.00995% / 0.02304% | 20.0 | 12000 | 58.53 |
| GBPCAD | FX | CAD | 0.01483% / 0.01854% | 30.0 | 12000 | 39.02 |
| GBPCHF | FX | CHF | 0.02346% / 0.02617% | 30.0 | 12000 | 39.02 |
| GBPJPY | FX | JPY | 0.00958% / 0.02347% | 30.0 | 12000 | 39.02 |
| GBPNZD | FX | NZD | 0.02123% / 0.03482% | 20.0 | 12000 | 58.53 |
| GBPUSD | FX | USD | 0.00754% / 0.01206% | 30.0 | 12000 | 39.02 |
| GOLD | METALS | USD | 0.00722% / 0.00746% | 10.0 | 12000 | 366.69 |
| Jp225 | INDEX | USD | 0.01323% / 0.01712% | 20.0 | 12000 | 148.79 |
| LCrude | ENERGY | USD | 0.04473% / 0.06709% | 10.0 | 12000 | 78.96 |
| LINKUSD | CRYPTO | USD | 0.55440% / 0.55440% | 2.0 | 12000 | 0.64 |
| NGas | ENERGY | USD | 0.76177% / 1.00416% | 9.9 | 12000 | 25.62 |
| NZDCAD | FX | CAD | 0.02869% / 0.06736% | 20.0 | 12000 | 24.86 |
| NZDCHF | FX | CHF | 0.04250% / 0.06587% | 20.0 | 12000 | 24.86 |
| NZDJPY | FX | JPY | 0.01805% / 0.03271% | 20.0 | 12000 | 24.86 |
| NZDUSD | FX | USD | 0.01776% / 0.02663% | 20.0 | 12000 | 24.86 |
| Palladium | METALS | USD | 0.80268% / 0.82580% | 10.0 | 12000 | 107.27 |
| Platinum | METALS | USD | 0.38706% / 0.59403% | 10.0 | 12000 | 151.20 |
| SGDJPY | FX | JPY | 0.02354% / 0.08604% | 20.0 | 12000 | 34.54 |
| SILVER | METALS | USD | 0.07454% / 0.08282% | 10.0 | 12000 | 266.54 |
| SOLUSD | CRYPTO | USD | 0.06763% / 0.10989% | 2.0 | 12000 | 0.52 |
| UNIUSD | CRYPTO | USD | 0.21226% / 0.21338% | 2.0 | 12000 | 0.39 |
| USDCAD | FX | CAD | 0.00702% / 0.00983% | 30.0 | 12000 | 29.42 |
| USDCHF | FX | CHF | 0.01197% / 0.01915% | 30.0 | 12000 | 29.42 |
| USDHUF | FX | HUF | 0.06926% / 0.28630% | 20.0 | 12000 | 44.13 |
| USDJPY | FX | JPY | 0.00318% / 0.00826% | 30.0 | 12000 | 29.42 |
| USDMXN | FX | MXN | 0.01376% / 0.05722% | 20.0 | 12000 | 44.13 |
| USDNOK | FX | NOK | 0.02899% / 0.10026% | 20.0 | 12000 | 44.13 |
| USDPLN | FX | PLN | 0.02831% / 0.12543% | 20.0 | 12000 | 44.13 |
| USDSEK | FX | SEK | 0.02688% / 0.13848% | 20.0 | 12000 | 44.13 |
| USDSGD | FX | SGD | 0.01722% / 0.03600% | 20.0 | 12000 | 44.13 |
| USDZAR | FX | ZAR | 0.08940% / 0.25579% | 20.0 | 12000 | 44.13 |
| Usa500 | INDEX | USD | 0.00547% / 0.01081% | 20.0 | 12000 | 169.39 |
| UsaInd | INDEX | USD | 0.00265% / 0.00690% | 20.0 | 12000 | 112.63 |
| UsaRus | INDEX | USD | 0.00963% / 0.00963% | 10.0 | 12000 | 123.78 |
| UsaTec | INDEX | USD | 0.00315% / 0.00492% | 20.0 | 12000 | 269.28 |
| XLMUSD | CRYPTO | USD | 0.78792% / 0.78792% | 2.0 | 12000 | 0.10 |
| XRPUSD | CRYPTO | USD | 0.67105% / 0.67105% | 1.3 | 12000 | 0.01 |

## PENDING quote re-check (46): all other gates passed; quote stale/absent because the market was closed at scan time

ABBN.CH, AIR.FR, ALV.GE, ASML.NE, AZN.UK, BAS.GE, BMW.GE, BNP.FR, BP.UK, Bra50, Brent, ChinaA50, Cocoa, Coffee, CoffeeR, Cotton, DTE.GE, Diesel, ENI.IT, Esp35, Euro50, Fra40, GLE.FR, Ger40, GerMid50, HSBA.UK, IBE.ES, INGA.NE, ISP.IT, ITX.ES, Ita40, MC.FR, NESN.CH, NOVN.CH, Neth25, OR.FR, SAN.ES, SAN.FR, SAP.GE, SHELL.NE, SIE.GE, Sugar, Swi20, UK100, USDBRL, USDCNH

## NOT_READY (excluded)

Top reason prefixes: not_sampled (326), trade_mode (40), dated_futures (38), quote_stale (26), no_quote (23), spread_median (7), eur_conversion (1)

- **not_sampled_stock_cfd** (326): A2.IT, AAL.UK, ABDN.UK, ABF.UK, ABI.BE, ABN.NE, AC.FR, ACA.FR, ACKB.BE, ACS.ES, ACX.ES, AD.NE, ADEN.CH, ADM.UK, ADS.GE, AEDAS.ES, AENA.ES, AF.FR, AGN.NE, AGS.BE, AI.FR, AIR.GE, AKZA.NE, ALC.CH, ALM.ES ... (+301)
- **dated_futures_contract_expiry_roll** (38): Bra50Oct26, BrentDec26, CarbonDec26, CocoaDec26, CoffeeDec26, CopperDec26, CornDec26, CottonDec26, Esp35Oct26, EuBTPDec26, EuBblDec26, EuBundDec26, EuStzDec26, Euro50Dec26, Fra40Oct26, GasolNov26, Ger40Dec26, Ita40Dec26, Jp225Dec26, LCrudeNov26, MinDolNov26, NGasNov26, OJNov26, OatsDec26, SoyMlDec26 ... (+13)
- **no_quote_since_market_watch_selection_market_closed_recheck_when_open** (23): ASML.NE, AZN.UK, BAS.GE, BMW.GE, BNP.FR, BP.UK, DTE.GE, ENI.IT, GLE.FR, HSBA.UK, IBE.ES, INGA.NE, ISP.IT, ITX.ES, MC.FR, NESN.CH, NOVN.CH, OR.FR, SAN.ES, SAN.FR, SAP.GE, SHELL.NE, SIE.GE
- **trade_mode_3_not_full** (23): ALFEN.NE, AML.UK, BOO.UK, BPOST.BE, CNE.UK, CPI.UK, ENC.ES, ERIC.GE, EURTRY, FP.FR, GEO.IT, GFT.GE, NA9.GE, OCI.NE, PHR.PO, PIA.IT, PNL.NE, SGE.UK, SMHN.GE, SMIN.UK, TLW.UK, TRYJPY, USDTRY
- **trade_mode_1_not_full** (12): ADYEN.NE, CAI.AT, CPI.AT, EMBR.SE, JUVE.IT, MAIRE.IT, POST.AT, PUIG.ES, R3NK.GE, SOON.CH, STMN.CH, TKA.AT
- **trade_mode_0_not_full** (5): USDAED, USDHKD, USDKWD, USDQAR, USDSAR
- **quote_stale_11118s_market_closed_or_halted** (2): Euro50, Ger40
- **quote_stale_11224s_market_closed_or_halted** (2): Fra40, Swi20
- **eur_conversion_not_derivable_HKD; quote_stale_14720s_market_closed_or_halted** (1): HKInd
- **quote_stale_11119s_market_closed_or_halted** (1): UK100
- **quote_stale_11120s_market_closed_or_halted** (1): Ita40
- **quote_stale_11226s_market_closed_or_halted** (1): Neth25
- **quote_stale_17120s_market_closed_or_halted** (1): Cotton
- **quote_stale_18509s_market_closed_or_halted** (1): Esp35
- **quote_stale_20113s_market_closed_or_halted** (1): Coffee
- **quote_stale_20119s_market_closed_or_halted** (1): Cocoa
- **quote_stale_23721s_market_closed_or_halted** (1): CoffeeR
- **quote_stale_25530s_market_closed_or_halted** (1): Sugar
- **quote_stale_27305s_market_closed_or_halted** (1): GerMid50
- **quote_stale_27323s_market_closed_or_halted** (1): AIR.FR
- **quote_stale_27327s_market_closed_or_halted** (1): ALV.GE
- **quote_stale_27925s_market_closed_or_halted** (1): ABBN.CH
- **quote_stale_6022s_market_closed_or_halted** (1): Bra50
- **quote_stale_6620s_market_closed_or_halted** (1): ChinaA50
- **quote_stale_7512s_market_closed_or_halted** (1): Diesel
- **quote_stale_7518s_market_closed_or_halted** (1): Brent
- **quote_stale_7521s_market_closed_or_halted** (1): USDCNH
- **quote_stale_7561s_market_closed_or_halted** (1): USDBRL
- **spread_median_0.3508%_of_price_too_wide; quote_stale_27307s_market_closed_or_halted** (1): GerTec
- **spread_median_1.6362%_of_price_too_wide** (1): LTCUSD
- **spread_median_1.8024%_of_price_too_wide; quote_stale_32970s_market_closed_or_halted** (1): USDRUB
- **spread_median_2.1037%_of_price_too_wide** (1): DOGEUSD
- **spread_median_3.6480%_of_price_too_wide** (1): AVAXUSD
- **spread_median_4.0290%_of_price_too_wide** (1): ADAUSD
- **spread_median_6.2745%_of_price_too_wide** (1): DOTUSD
