from fastapi import FastAPI, Depends, HTTPException, status, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
import os
import shutil
import logging

logger = logging.getLogger(__name__)

from app.database import get_db
from app.config import settings
from app.services.reconciliation import ReconciliationService
from app.services.invoice_parser import InvoiceParserService
from app.services.email_fetcher import EmailFetcherService
from app.services.bank_statement_parser import BankStatementParserService
from app.models.bank import BankAccount, BankTransaction
from app.models.accounting import AccountingEntry
from app.models.category import Category
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

app = FastAPI(
    title="AppGastos API",
    description="API de Contabilidad Analítica y Optimización Financiera Personal",
    version="1.0.0"
)

# Configuración de CORS para permitir la conexión desde el Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, restringir a los dominios del frontend
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {
        "message": "Bienvenido a la API de AppGastos",
        "version": "1.0.0",
        "status": "online"
    }

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check(db: Session = Depends(get_db)):
    """
    Endpoint de comprobación de salud de la aplicación.
    Verifica que la conexión a la base de datos PostgreSQL funcione correctamente.
    """
    try:
        # Ejecuta una consulta simple para comprobar la salud de la DB
        db.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "database": "connected",
            "environment": "debug" if settings.DEBUG else "production"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Error de conexión con la base de datos: {str(e)}"
        )

@app.post("/users/{user_id}/reconcile", status_code=status.HTTP_200_OK)
def trigger_reconciliation(user_id: str, db: Session = Depends(get_db)):
    """
    Ejecuta manualmente el motor de conciliación y desglose de gastos
    para las transacciones del usuario especificado.
    """
    try:
        service = ReconciliationService(db)
        reconciled_count = service.reconcile_user_transactions(user_id=user_id)
        return {
            "status": "success",
            "message": f"Proceso de conciliación completado para el usuario {user_id}.",
            "reconciled_transactions_count": reconciled_count
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error durante el proceso de conciliación: {str(e)}"
        )

@app.post("/users/{user_id}/invoices/upload", status_code=status.HTTP_201_CREATED)
def upload_invoice(user_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Sube un PDF de factura, extrae su texto, analiza con Gemini para mapear
    los campos e ítems, guarda en la base de datos e inicia el matching bancario.
    """
    # 1. Validar formato de archivo
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se admiten archivos en formato PDF."
        )

    # 2. Crear directorio temporal si no existe
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    
    file_path = os.path.join(temp_dir, f"{user_id}_{file.filename}")

    # 3. Guardar archivo en disco temporalmente
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al escribir el archivo temporal: {str(e)}"
        )

    # 4. Procesar el PDF con el InvoiceParserService
    try:
        parser = InvoiceParserService(db)
        invoice = parser.process_and_reconcile(user_id=user_id, file_path=file_path)
        
        # Consultar si el registro guardado terminó conciliado
        reconciled = invoice.accounting_entries[0].bank_transaction_id is not None if invoice.accounting_entries else False

        return {
            "status": "success",
            "message": "Factura procesada y guardada correctamente.",
            "invoice_id": invoice.id,
            "emitter": invoice.emitter_name,
            "total_amount": invoice.total_amount,
            "currency": invoice.currency,
            "issue_date": invoice.issue_date,
            "items_count": len(invoice.items),
            "reconciled": reconciled
        }
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al procesar la factura con IA: {str(e)}"
        )
    finally:
        # 5. Limpieza del archivo temporal
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"No se pudo eliminar el archivo temporal {file_path}: {str(e)}")


@app.post("/users/{user_id}/emails/sync", status_code=status.HTTP_200_OK)
def sync_emails(user_id: str, db: Session = Depends(get_db)):
    """
    Se conecta al servidor IMAP configurado en las variables de entorno,
    escanea los emails no leídos que contienen palabras clave analíticas,
    extrae e interpreta facturas (de PDFs o del cuerpo HTML) e intenta conciliarlas.
    """
    try:
        fetcher = EmailFetcherService(db)
        processed_emails = fetcher.fetch_inbound_invoices(user_id=user_id)
        
        return {
            "status": "success",
            "message": f"Sincronización de correos completada. Se procesaron {len(processed_emails)} correos con facturas.",
            "processed_count": len(processed_emails),
            "processed_emails": processed_emails
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error durante la sincronización de correos: {str(e)}"
        )


@app.post("/users/{user_id}/emails/upload-eml", status_code=status.HTTP_201_CREATED)
def upload_eml(user_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Sube un archivo de correo .eml, extrae las facturas adjuntas o el cuerpo del email,
    y procesa y concilia las transacciones correspondientes.
    """
    # 1. Validar formato de archivo
    if not file.filename.lower().endswith('.eml'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se admiten archivos en formato .eml"
        )

    # 2. Crear directorio temporal si no existe
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    
    file_path = os.path.join(temp_dir, f"eml_{user_id}_{file.filename}")

    # 3. Guardar archivo en disco temporalmente
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al escribir el archivo temporal: {str(e)}"
        )

    # 4. Procesar el EML con el EmailFetcherService
    try:
        fetcher = EmailFetcherService(db)
        processed_emails = fetcher.parse_eml_file(user_id=user_id, file_path=file_path)
        
        return {
            "status": "success",
            "message": f"Archivo de correo procesado. Se detectaron {len(processed_emails)} facturas.",
            "processed_count": len(processed_emails),
            "processed_emails": processed_emails
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al procesar el archivo .eml con IA: {str(e)}"
        )
    finally:
        # 5. Limpieza del archivo temporal
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"No se pudo eliminar el archivo temporal {file_path}: {str(e)}")


# ============================================================================
# NUEVOS ENDPOINTS: CUENTAS BANCARIAS Y EXTRACTOS
# ============================================================================

class BankAccountCreate(BaseModel):
    provider_name: str
    account_number_masked: str
    account_type: str
    balance: float = 0.0
    currency: str = "EUR"


@app.get("/users/{user_id}/accounts")
def get_accounts(user_id: str, db: Session = Depends(get_db)):
    """Lista todas las cuentas bancarias de un usuario."""
    return db.query(BankAccount).filter(BankAccount.user_id == user_id).all()


@app.post("/users/{user_id}/accounts", status_code=status.HTTP_201_CREATED)
def create_account(user_id: str, account_data: BankAccountCreate, db: Session = Depends(get_db)):
    """Crea una nueva cuenta bancaria para el usuario."""
    account = BankAccount(
        user_id=user_id,
        provider_name=account_data.provider_name,
        account_number_masked=account_data.account_number_masked,
        account_type=account_data.account_type,
        balance=account_data.balance,
        currency=account_data.currency
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@app.put("/users/{user_id}/accounts/{account_id}")
def update_account(user_id: str, account_id: str, account_data: BankAccountCreate, db: Session = Depends(get_db)):
    """Actualiza una cuenta bancaria existente."""
    account = db.query(BankAccount).filter(
        BankAccount.user_id == user_id,
        BankAccount.id == account_id
    ).first()

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cuenta bancaria no encontrada."
        )

    account.provider_name = account_data.provider_name
    account.account_number_masked = account_data.account_number_masked
    account.account_type = account_data.account_type
    account.balance = account_data.balance
    account.currency = account_data.currency

    db.commit()
    db.refresh(account)
    return account


@app.post("/users/{user_id}/accounts/{account_id}/statements/upload", status_code=status.HTTP_201_CREATED)
def upload_bank_statement(user_id: str, account_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Sube y procesa un extracto bancario (PDF/CSV/Excel) asociándolo a una cuenta."""
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ['.pdf', '.csv', '.xlsx', '.xls', '.txt']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formato de extracto no admitido (se admiten PDF, CSV, Excel o TXT)."
        )
        
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    file_path = os.path.join(temp_dir, f"statement_{account_id}_{file.filename}")
    
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        parser = BankStatementParserService(db)
        statement_data = parser.parse_statement_file(file_path)
        imported_count = parser.save_and_process(user_id, account_id, statement_data)
        
        return {
            "status": "success",
            "message": f"Extracto bancario procesado correctamente. Importadas {imported_count} transacciones.",
            "imported_count": imported_count
        }
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error procesando extracto bancario: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


@app.get("/users/{user_id}/entries")
def get_entries(user_id: str, account_id: Optional[str] = None, db: Session = Depends(get_db)):
    """Obtiene los asientos del libro mayor del usuario, filtrados opcionalmente por cuenta bancaria."""
    query = db.query(AccountingEntry).filter(AccountingEntry.user_id == user_id)
    
    if account_id and account_id != "all":
        query = query.join(AccountingEntry.bank_transaction).filter(
            BankTransaction.bank_account_id == account_id
        )
        
    entries = query.order_by(AccountingEntry.entry_date.desc()).all()
    
    # Pre-calcular splits buscando transacciones bancarias compartidas por más de un asiento
    tx_counts = {}
    if entries:
        tx_ids = [e.bank_transaction_id for e in entries if e.bank_transaction_id]
        if tx_ids:
            from sqlalchemy import func
            counts = db.query(
                AccountingEntry.bank_transaction_id, 
                func.count(AccountingEntry.id)
            ).filter(
                AccountingEntry.bank_transaction_id.in_(tx_ids)
            ).group_by(AccountingEntry.bank_transaction_id).all()
            tx_counts = {t_id: count for t_id, count in counts if t_id is not None}

    result = []
    for e in entries:
        has_split = False
        if e.bank_transaction_id:
            has_split = tx_counts.get(e.bank_transaction_id, 0) > 1
            
        # Determinar el orderId para que el frontend pueda abrir desgloses
        assoc_id = None
        if e.amazon_order_id:
            assoc_id = str(e.amazon_order_id)
        elif e.invoice_id:
            assoc_id = str(e.invoice_id)

        result.append({
            "id": str(e.id),
            "date": e.entry_date.strftime("%Y-%m-%d"),
            "merchant": e.description,
            "category": e.category.name,
            "parentCategory": e.category.parent.name if e.category.parent else "Ninguno",
            "source": "Factura PDF" if e.invoice_id else ("Amazon" if e.amazon_order_id else "Banco"),
            "method": "Fuzzy Matching (IA)" if e.reconciliation_type in ['MATCHED_INVOICE', 'SPLIT_AMAZON'] else "Directo Banco",
            "amount": float(e.amount),
            "hasSplit": has_split,
            "orderId": assoc_id
        })
    return result


@app.get("/users/{user_id}/metrics")
def get_metrics(user_id: str, account_id: Optional[str] = None, db: Session = Depends(get_db)):
    """Retorna métricas y datos de gráficos calculados a partir de los datos de la DB."""
    entries_query = db.query(AccountingEntry).filter(AccountingEntry.user_id == user_id)
    tx_query = db.query(BankTransaction).join(BankTransaction.bank_account).filter(BankAccount.user_id == user_id)
    
    if account_id and account_id != "all":
        entries_query = entries_query.join(AccountingEntry.bank_transaction).filter(
            BankTransaction.bank_account_id == account_id
        )
        tx_query = tx_query.filter(BankTransaction.bank_account_id == account_id)
        
    entries = entries_query.all()
    txs = tx_query.all()
    
    # Calcular total de gastos del mes de Julio 2026
    current_month_expenses = sum(float(e.amount) for e in entries if e.amount < 0 and e.entry_date.year == 2026 and e.entry_date.month == 7)
    
    # Tasa de conciliación
    total_tx_count = len(txs)
    reconciled_tx_count = sum(1 for t in txs if t.is_reconciled)
    reconcile_rate = (reconciled_tx_count / total_tx_count * 100) if total_tx_count > 0 else 100
    
    # Fugas de dinero (suscripciones inactivas/duplicadas simuladas sobre datos analíticos)
    leaks_count = 1 if len(txs) > 0 else 0
    
    # Distribución por categorías (Doughnut)
    cat_dist = {}
    for e in entries:
        if e.amount < 0:
            cat_name = e.category.name
            cat_dist[cat_name] = cat_dist.get(cat_name, 0) + float(abs(e.amount))
            
    # Histórico de evolución de gastos (Line Chart)
    month_names = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul']
    month_data = [0.0] * 7
    for e in entries:
        if e.amount < 0 and e.entry_date.year == 2026:
            m_idx = e.entry_date.month - 1
            if 0 <= m_idx < 7:
                month_data[m_idx] += float(abs(e.amount))
                
    return {
        "totalExpenses": f"{abs(current_month_expenses):.2f} €",
        "reconcileRate": f"{reconcile_rate:.0f}%",
        "moneyLeaks": f"{leaks_count} Activa" if leaks_count > 0 else "0 Activas",
        "investCapital": f"{abs(current_month_expenses) * 0.15:.2f} €", # 15% del gasto estimado optimizable
        "categoryDistribution": cat_dist,
        "evolution": {
            "labels": month_names,
            "expenses": month_data
        }
    }


@app.get("/users/{user_id}/splits/{order_id}")
def get_splits(user_id: str, order_id: str, db: Session = Depends(get_db)):
    """Obtiene el desglose detallado (split) de artículos para un pedido o factura."""
    entries = db.query(AccountingEntry).filter(
        AccountingEntry.user_id == user_id,
        (AccountingEntry.amazon_order_id == order_id) | (AccountingEntry.invoice_id == order_id)
    ).all()
    
    if not entries:
        raise HTTPException(status_code=404, detail="No se encontraron desgloses para este identificador.")
        
    items = []
    tx_total = 0.0
    tx_date = None
    merchant = ""
    
    for e in entries:
        tx_date = e.entry_date.strftime("%Y-%m-%d")
        if e.bank_transaction:
            tx_total = float(abs(e.bank_transaction.amount))
            merchant = e.bank_transaction.cleaned_merchant or e.bank_transaction.raw_description
            
        items.append({
            "title": e.description,
            "qty": 1,
            "price": f"{abs(e.amount):.2f} €",
            "cat": f"{e.category.parent.name if e.category.parent else ''} -> {e.category.name}"
        })
        
    return {
        "txDate": tx_date,
        "txTotal": f"-{tx_total:.2f} €",
        "merchant": merchant,
        "items": items
    }



