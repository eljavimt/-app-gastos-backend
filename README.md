# Backend - AppGastos (FastAPI)

Este directorio contiene la lógica de negocio, los modelos de datos de base de datos de PostgreSQL (SQLAlchemy) y el motor de conciliación y desglose de gastos para la plataforma AppGastos.

## Estructura de Directorios

*   `app/config.py`: Carga y validación de variables de entorno mediante `pydantic-settings`.
*   `app/database.py`: Configuración de SQLAlchemy y manejador de sesión `get_db()`.
*   `app/models/`: Modelado ORM completo (Usuarios, Cuentas Bancarias, Transacciones, Facturas, Amazon y Asientos Contables).
*   `app/services/reconciliation.py`: Algoritmo de emparejamiento con puntuación ponderada y soporte para desglose de transacciones (splits).
*   `app/main.py`: Puntos de entrada HTTP expuestos (FastAPI).

---

## Requisitos y Configuración

### 1. Preparar Entorno Virtual

Es recomendable crear un entorno virtual de Python para instalar las dependencias:

```bash
# Crear entorno virtual en el directorio root del backend
python -m venv venv

# Activar en Windows (PowerShell)
.\venv\Scripts\Activate.ps1

# Activar en Linux/macOS
source venv/bin/activate
```

### 2. Instalar Dependencias

```bash
pip install -r requirements.txt
```

### 3. Variables de Entorno

El archivo `.env` ya ha sido pre-generado en este directorio con valores por defecto. Si necesitas cambiar las credenciales de tu base de datos o añadir claves de APIs de LLMs (como Gemini, OpenAI o Anthropic), puedes modificar el archivo:

*   `DATABASE_URL`: URL de conexión de PostgreSQL.
*   `ENCRYPTION_KEY`: Clave AES de 32 bytes en formato base64 para cifrar los datos sensibles del usuario.

---

## Ejecutar el Servidor de Desarrollo

Una vez configuradas las dependencias y la base de datos, ejecuta el servidor mediante `uvicorn`:

```bash
uvicorn app.main:app --reload --port 8000
```

*   **API interactiva (Swagger UI):** [http://localhost:8000/docs](http://localhost:8000/docs)
*   **Comprobación de salud:** [http://localhost:8000/health](http://localhost:8000/health)

---

## Detalle del Motor de Conciliación

El servicio de conciliación expone una función centralizada:
`ReconciliationService.reconcile_user_transactions(user_id)`

Esta función:
1.  Obtiene los cargos bancarios no conciliados de la base de datos.
2.  Busca facturas (invoices) y compras de Amazon registradas en un rango de fecha de **±3 días**.
3.  Aplica una puntuación de coincidencia que exige la exactitud en el importe, recompensa la cercanía temporal y calcula una similitud difusa (fuzzy match) del nombre del emisor/comercio usando **Jaro-Winkler** (vía la librería `rapidfuzz`).
4.  Si se supera el umbral de aceptación (75%), asocia la transacción e inserta los asientos correspondientes en el libro mayor (`accounting_entries`), dividiendo los importes artículo por artículo (Splits) cuando la compra proviene de Amazon.
