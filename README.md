# mp-gastos

Llevá un registro local de tus gastos de MercadoPago de Argentina. Podés importar el reporte **Todas las transacciones**, consultar tus compras con tarjeta, revisar movimientos y generar un resumen mensual en Markdown. Los datos se guardan en SQLite en tu computadora. El paquete requiere Python 3.11 o superior.

## Instalación

Desde la carpeta del repositorio, en PowerShell, ejecutá lo siguiente.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
mp-gastos init
```

En macOS o Linux, activá el entorno con `source .venv/bin/activate`. El comando `init` crea la base y el archivo de reglas en `.mp-gastos/`. Si volvés a ejecutarlo, conserva tus reglas y movimientos. Usá siempre la misma carpeta de trabajo, o indicá una ruta fija con `mp-gastos --home RUTA comando`. Usá una carpeta distinta para cada cuenta.

## Creá la aplicación y obtené tu Access Token

1. Entrá a Mercado Pago Developers con la cuenta argentina cuyos movimientos querés consultar. Abrí **Tus integraciones** y elegí **Crear aplicación**. [Panel de integraciones](https://www.mercadopago.com.ar/developers/panel/app)
2. Elegí la creación manual y poné un nombre que reconozcas, por ejemplo `Mis gastos locales`. Si el asistente te pide una integración, elegí **Pagos online** y una integración propia, como **Checkout API**. Completá los datos que te solicite y confirmá la creación. [Guía de creación de aplicaciones](https://www.mercadopago.com.ar/developers/es/docs/checkout-api-orders/create-application)
3. Abrí la aplicación y entrá en **Producción → Credenciales de producción** o **Credenciales → Productivas**. Si aparece **Activar credenciales**, completá el rubro y la URL de tu sitio que pide MP, aceptá las condiciones y confirmá. [Activación de credenciales](https://www.mercadopago.com.ar/developers/es/docs/checkout-pro-preferences/go-to-production)
4. Copiá el valor del campo **Access Token** de producción. En la carpeta desde la que vas a correr el comando, copiá `.env.example` a `.env` y pegá el valor a la derecha de `MP_ACCESS_TOKEN=`. Guardá el archivo. Si ya definiste esa variable en el entorno, tiene prioridad.

**El token da acceso a tu cuenta.** Guardalo como una contraseña. No lo compartas, no lo subas a GitHub ni lo pegues en una captura, un chat o un issue. Si quedó expuesto, renovalo en el panel. La CLI lee el token sin mostrarlo. La base, los archivos descargados y los resúmenes también contienen información personal.

## Sincronizá y revisá

```console
mp-gastos sync
mp-gastos sync --month 2026-04 --months 3
mp-gastos review
mp-gastos review --id 6 --kind purchase --category "Materiales"
mp-gastos report --month 2026-04 --output abril.md
mp-gastos report --month 2026-04 --json --output abril.json
mp-gastos review --json
```

`--month` indica el último mes a consultar. El segundo ejemplo recorre febrero, marzo y abril. Elegí fechas entre el mes actual y los once anteriores. Las ventanas siguen el horario argentino. Para probar el flujo sin credenciales, usá el CSV inventado del repositorio.

```console
mp-gastos init
mp-gastos sync --csv tests/fixtures/settlement.csv
mp-gastos review
mp-gastos report --month 2026-04
```

La sincronización consulta la configuración del reporte y solicita el período a Mercado Pago. Si la cuenta todavía carece de configuración, crea una con separador `;` y encabezados en inglés. Si ya tenés una, verificá que use ese separador y las columnas `SOURCE_ID`, `TRANSACTION_DATE`, `TRANSACTION_TYPE`, `TRANSACTION_CURRENCY` y `SETTLEMENT_NET_AMOUNT`. Agregá `DESCRIPTION`, `EXTERNAL_REFERENCE` y `PAYMENT_METHOD_TYPE` para aprovechar las reglas. La sincronización conserva la programación remota. [Reportes por API](https://www.mercadopago.com.ar/developers/es/docs/links-and-debts/additional-content/reports/account-money/api)

El sync espera hasta **180 segundos** por reporte. Si sigue pendiente, devuelve código **2** y guarda la tarea. Volvé a correrlo con el mismo mes para retomar la descarga. Con `--wait 0` consulta una vez y devuelve el control. El código **0** indica éxito y **1** indica un error operativo. Las compras con tarjeta se actualizan incluso si el reporte queda pendiente.

### Combiná el mensual con reportes diarios

Descargá los CSV diarios de **Todas las transacciones** y pasá un `--daily-csv` por archivo. Podés importar los diarios solos, antes o después del mensual descargado por `sync`, o cargar todo junto.

```console
mp-gastos sync --daily-csv tests/fixtures/settlement-daily.csv
mp-gastos sync --csv tests/fixtures/settlement-monthly.csv --daily-csv tests/fixtures/settlement-daily.csv
```

La base deduplica por `SOURCE_ID`, tipo de operación, fecha y hora, moneda e importe neto. Conserva importes distintos para un mismo ID y respeta la cantidad de veces que una fila idéntica aparece en un mismo CSV. También conserva los campos que aporta un archivo cuando vienen vacíos en el otro. Si una fila aparece únicamente en los diarios dentro del período cubierto por un mensual importado, `sync` muestra un aviso en stderr y el informe mensual muestra el recuento. En JSON se llama `daily_only_movements`. Si el mensual está vacío, pasá `--month AAAA-MM` junto con `--csv` para registrar qué mes cubre.

Si el archivo incluye `COUNTERPARTY`, se usa como contraparte y sirve como descripción para las reglas cuando `DESCRIPTION` viene vacía. El sync por API busca los IDs numéricos sin contraparte en la API de pagos. Usa el nombre que devuelva MP cuando está disponible y puede aprovechar la descripción o el ID de la otra parte. Para habilitar esa consulta al cargar archivos locales, pasá `--enrich` con el token de la misma cuenta. Las filas cuyo detalle MP no encuentra o no permite consultar se importan con los datos disponibles. [API de consulta de pagos](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/get-payment/get)

### Ajustá y reaplicá las reglas

En la salida JSON de `report` y `review`, los campos que terminan en `_cents` expresan centavos enteros. En `report`, cada moneda tiene su propio `to_add_cents`, que corresponde a **Para sumar al resumen**. Cuando falta el total del comprador, `paid_cents` vale `null`.

En `.mp-gastos/rules.toml`, elegí un campo entre `description`, `counterparty`, `transaction_type`, `external_reference`, `payment_method_type` e `impact_kind`. Este último toma el valor de `IMPACT_KIND` cuando el CSV trae esa columna. Cada regla usa un operador. `contains` busca el texto en cualquier posición, `exact` compara el valor completo y `prefix` compara el comienzo. Se aplica la primera coincidencia, ignorando mayúsculas y acentos. Podés restringir la regla a `debit` o `credit` y elegir la categoría.

```toml
[[rule]]
field = "counterparty"
exact = "Financiera Ámbar"
kind = "loan"
category = "Cuotas de préstamo"
direction = "debit"

[[rule]]
field = "impact_kind"
prefix = "asset_management"
kind = "yield"
category = "Inversiones"
direction = "credit"
```

Si ninguna regla coincide, un débito con referencia que empieza con `ccpaymentprod_` o `cctiprod-` se clasifica como pago de tarjeta. Un crédito `SETTLEMENT` se clasifica como rendimiento cuando sus datos lo identifican como tal, por ejemplo con `Rendimiento` o `asset_management_gain`. Los movimientos ambiguos quedan en `review`. Estos son los tipos disponibles.

```text
purchase  transfer  card_payment  yield  fee  refund  withdrawal  loan  other
```

Después de editar las reglas, ejecutá `mp-gastos reclassify`. El comando usa los datos guardados y respeta las clasificaciones manuales. La base se actualiza automáticamente al abrirla. Para recuperar columnas que la versión anterior descartaba o filas que se habían confundido al importar, cargá de nuevo los CSV originales.

## Conciliá con el resumen de tu tarjeta

El informe lista los ID de pago y agrupa las compras por emisor y moneda. Toma el importe total pagado por el comprador, incluidos los cargos informados por MP. Cuando MP omite ese total, la compra aparece como pendiente de importe. Las transferencias, los rendimientos, los préstamos y el pago de la tarjeta quedan fuera del gasto de consumo.

Para mostrar el nombre del emisor, cargá sus códigos en la tabla `[issuers]` de `.mp-gastos/rules.toml`. Podés escribir los códigos numéricos con o sin comillas. Los emisores sin nombre configurado se muestran con su código.

```toml
[issuers]
71 = "Banco de ejemplo"
"72" = "Otro banco de ejemplo"
```

Buscá el consumo en el resumen del emisor. Una vez identificado, ingresá el ID de pago de MP, una referencia única para esa línea, el importe del resumen y su moneda. Por ejemplo, para la primera cuota de una compra ficticia de ARS 23,45:

```console
mp-gastos reconcile --payment 71002 --line "Emisor71-2026-04-renglon08" --amount 12.00 --currency ARS
mp-gastos report --month 2026-04
```

Podés vincular otra cuota con su propia referencia. Repetir la misma línea es seguro. El informe asigna la compra al mes de la operación y muestra cuánto ya figura en los resúmenes vinculados, aunque lleguen después. Si llevás otro registro con los consumos del banco, agregale solo la columna **Para sumar al resumen**. En el ejemplo quedarían ARS 11,45 de la compra por conciliar. La tabla general también incluye los gastos confirmados de la cuenta. Si una compra con tarjeta aparece en el reporte de cuenta con el mismo ID, se cuenta una sola vez.

Para cargar varias conciliaciones juntas, ejecutá `mp-gastos reconcile --import conciliaciones.csv`. Usá un CSV UTF-8 separado por comas. Si cada línea cubre la compra completa, alcanzan el ID del pago y una referencia única. El comando toma el total y la moneda de la compra sincronizada.

```csv
payment,line
71002,Emisor71-2026-04-renglon08
```

Para cuotas o importes parciales, incluí las cuatro columnas en todo el archivo.

```csv
payment,line,amount,currency
71002,Emisor71-2026-04-renglon08,12.00,ARS
71002,Emisor71-2026-05-renglon03,11.45,ARS
```

Si hay una referencia en conflicto, una moneda distinta o un importe que excede el total de la compra, se descarta el lote completo. Podés corregir el archivo y volver a importarlo. Las líneas idénticas se cuentan una sola vez.

## Uso como librería

```python
from pathlib import Path

from mp_gastos.client import MercadoPagoClient
from mp_gastos.ledger import database, initialize
from mp_gastos.report import monthly_report
from mp_gastos.settlements import load_issuers
from mp_gastos.sync import sync

home = Path(".mp-gastos")
initialize(home)
with database(home) as db:
    states = sync(db, MercadoPagoClient(), "2026-04", rules_path=home / "rules.toml")
    print(states)
    print(monthly_report(db, "2026-04", load_issuers(home / "rules.toml")))
```

## Desarrollo

```console
python -m pip install -e .
python -m unittest discover -s tests -v
python -m pip install build twine
python -m build
python -m twine check dist/*
```

Los tests usan fixtures inventados y transporte simulado. GitHub Actions prueba Linux y Windows y verifica el wheel y la distribución fuente. Licencia MIT.
