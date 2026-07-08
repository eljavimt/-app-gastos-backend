import logging
from datetime import timedelta, date
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models.bank import BankTransaction
from app.models.invoice import Invoice
from app.models.amazon import AmazonOrder
from app.models.accounting import AccountingEntry
from app.models.category import Category
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

class ReconciliationService:
    def __init__(self, db: Session):
        self.db = db

    def calculate_match_score(
        self,
        tx_amount: float,
        doc_amount: float,
        tx_date: date,
        doc_date: date,
        tx_name: str,
        doc_name: str
    ) -> float:
        """
        Calcula un score de coincidencia ponderado entre una transacción bancaria y un documento (factura/pedido).
        Importe exacto: 50%
        Proximidad de fechas (+/- 3 días): 25%
        Similitud de texto (Jaro-Winkler): 25%
        """
        # A. Coincidencia de Importe (Peso: 50%)
        amount_diff = abs(tx_amount - doc_amount)
        if amount_diff == 0:
            amount_score = 1.0
        elif amount_diff < 0.05:  # Margen de centavos por redondeos
            amount_score = 0.9
        else:
            # Si el importe difiere significativamente, no se puede conciliar automáticamente
            return 0.0

        # B. Proximidad de Fechas (Peso: 25%)
        days_diff = abs((tx_date - doc_date).days)
        if days_diff == 0:
            date_score = 1.0
        elif days_diff == 1:
            date_score = 0.8
        elif days_diff == 2:
            date_score = 0.5
        elif days_diff == 3:
            date_score = 0.2
        else:
            date_score = 0.0

        # C. Similitud de Texto (Fuzzy Matching Jaro-Winkler) (Peso: 25%)
        # Limpieza simple de textos
        s1 = tx_name.lower().strip()
        s2 = doc_name.lower().strip()
        
        # Similitud en rango [0, 100] de rapidfuzz convertida a [0.0, 1.0]
        text_similarity = fuzz.jaro_winkler(s1, s2) / 100.0

        # Puntuación final ponderada
        final_score = (amount_score * 0.50) + (date_score * 0.25) + (text_similarity * 0.25)
        return final_score

    def reconcile_user_transactions(self, user_id: str, date_margin_days: int = 3):
        """
        Busca transacciones bancarias no reconciliadas del usuario y las cruza
        con facturas y pedidos de Amazon candidatos.
        """
        # 1. Obtener transacciones no reconciliadas de cuentas del usuario
        unreconciled_txs = self.db.query(BankTransaction).join(
            BankTransaction.bank_account
        ).filter(
            BankTransaction.is_reconciled == False,
            BankTransaction.amount < 0  # Usualmente conciliamos cargos (gastos)
        ).all()

        reconciled_count = 0

        for tx in unreconciled_txs:
            best_match = None
            highest_score = 0.0
            match_type = None  # 'INVOICE' o 'AMAZON'

            # Margen temporal
            start_date = tx.transaction_date - timedelta(days=date_margin_days)
            end_date = tx.transaction_date + timedelta(days=date_margin_days)

            # --- BUSCAR FACTURAS CANDIDATAS ---
            candidate_invoices = self.db.query(Invoice).filter(
                Invoice.user_id == user_id,
                Invoice.issue_date.between(start_date, end_date)
            ).all()

            for inv in candidate_invoices:
                score = self.calculate_match_score(
                    tx_amount=float(abs(tx.amount)),
                    doc_amount=float(inv.total_amount),
                    tx_date=tx.transaction_date,
                    doc_date=inv.issue_date,
                    tx_name=tx.raw_description,
                    doc_name=inv.emitter_name
                )
                if score > highest_score and score >= 0.75:
                    highest_score = score
                    best_match = inv
                    match_type = 'INVOICE'

            # --- BUSCAR PEDIDOS DE AMAZON CANDIDATOS ---
            # (Solo si no encontramos una factura perfecta o si queremos comparar puntajes)
            candidate_orders = self.db.query(AmazonOrder).filter(
                AmazonOrder.user_id == user_id,
                AmazonOrder.order_date.between(start_date, end_date)
            ).all()

            for order in candidate_orders:
                score = self.calculate_match_score(
                    tx_amount=float(abs(tx.amount)),
                    doc_amount=float(order.total_amount),
                    tx_date=tx.transaction_date,
                    doc_date=order.order_date,
                    tx_name=tx.raw_description,
                    doc_name="Amazon"
                )
                if score > highest_score and score >= 0.75:
                    highest_score = score
                    best_match = order
                    match_type = 'AMAZON'

            # 2. Ejecutar la conciliación física si se superó el umbral
            if best_match:
                try:
                    self._apply_reconciliation(tx, best_match, match_type, highest_score, user_id)
                    reconciled_count += 1
                except Exception as e:
                    self.db.rollback()
                    logger.error(f"Error conciliando transaccion {tx.id}: {str(e)}")

        return reconciled_count

    def _apply_reconciliation(self, tx: BankTransaction, doc, match_type: str, score: float, user_id: str):
        """
        Crea los asientos contables en la tabla accounting_entries (con soporte de desgloses/splits)
        y marca el movimiento bancario como reconciliado.
        """
        # Obtener una categoría genérica por defecto por si no se puede clasificar individualmente
        default_category = self.db.query(Category).filter(Category.code == 'OCI_TECNOLOGIA').first()
        default_cat_id = default_category.id if default_category else None

        if match_type == 'AMAZON':
            # Si es pedido de Amazon, intentamos desglosar por artículos
            items = doc.items
            if items:
                # Split Transaction: Crear un asiento por cada artículo
                for item in items:
                    # Intentar resolver categoría del artículo
                    cat_id = self._resolve_amazon_category(item.amazon_category) or default_cat_id
                    
                    entry = AccountingEntry(
                        user_id=user_id,
                        category_id=cat_id,
                        bank_transaction_id=tx.id,
                        amazon_order_id=doc.id,
                        amazon_item_id=item.id,
                        entry_date=doc.order_date,
                        amount=-float(item.total_price),  # Negativo ya que es un gasto
                        description=f"Amazon: {item.product_title} ({item.seller_name})",
                        reconciliation_type='SPLIT_AMAZON',
                        confidence_score=score,
                        metadata_json={
                            "seller": item.seller_name,
                            "amazon_category": item.amazon_category,
                            "quantity": item.quantity
                        }
                    )
                    self.db.add(entry)
            else:
                # Si no tiene ítems cargados, asiento único
                entry = AccountingEntry(
                    user_id=user_id,
                    category_id=default_cat_id,
                    bank_transaction_id=tx.id,
                    amazon_order_id=doc.id,
                    entry_date=doc.order_date,
                    amount=-float(doc.total_amount),
                    description="Pedido Amazon (Sin desglose de productos)",
                    reconciliation_type='MATCHED_INVOICE',
                    confidence_score=score
                )
                self.db.add(entry)

        elif match_type == 'INVOICE':
            # Si es factura, vemos si tiene líneas de desglose
            items = doc.items
            if items:
                for item in items:
                    cat_id = self._resolve_invoice_item_category(item.description) or default_cat_id
                    entry = AccountingEntry(
                        user_id=user_id,
                        category_id=cat_id,
                        bank_transaction_id=tx.id,
                        invoice_id=doc.id,
                        entry_date=doc.issue_date,
                        amount=-float(item.total_amount),
                        description=f"{doc.emitter_name}: {item.description}",
                        reconciliation_type='MATCHED_INVOICE',
                        confidence_score=score,
                        metadata_json={"emitter_tax_id": doc.emitter_tax_id}
                    )
                    self.db.add(entry)
            else:
                # Intentar categorizar por el nombre del emisor
                cat_id = self._resolve_emitter_category(doc.emitter_name) or default_cat_id
                entry = AccountingEntry(
                    user_id=user_id,
                    category_id=cat_id,
                    bank_transaction_id=tx.id,
                    invoice_id=doc.id,
                    entry_date=doc.issue_date,
                    amount=-float(doc.total_amount),
                    description=f"Factura {doc.emitter_name}",
                    reconciliation_type='MATCHED_INVOICE',
                    confidence_score=score,
                    metadata_json={"emitter_tax_id": doc.emitter_tax_id}
                )
                self.db.add(entry)

        # 3. Marcar la transacción bancaria como reconciliada
        tx.is_reconciled = True
        self.db.commit()

    # --- RESOLUCIÓN HEURÍSTICA DE CATEGORÍAS ---

    def _resolve_amazon_category(self, amazon_cat: str) -> str:
        """Mapea categorías nativas de Amazon a IDs de categorías del sistema."""
        if not amazon_cat:
            return None
        cat_lower = amazon_cat.lower()
        
        # Mapeos rápidos
        if "book" in cat_lower or "libro" in cat_lower:
            code = 'EDU_LIBROS'
        elif "elec" in cat_lower or "computer" in cat_lower or "tech" in cat_lower:
            code = 'OCI_TECNOLOGIA'
        elif "clothing" in cat_lower or "ropa" in cat_lower or "shoe" in cat_lower:
            code = 'OCI_ROPA'
        elif "food" in cat_lower or "comida" in cat_lower or "grocer" in cat_lower:
            code = 'ALI_SUPER'
        else:
            return None
            
        category = self.db.query(Category).filter(Category.code == code).first()
        return category.id if category else None

    def _resolve_invoice_item_category(self, desc: str) -> str:
        """Resuelve categoría basada en palabras clave en la descripción del ítem."""
        desc_lower = desc.lower()
        if "luz" in desc_lower or "electricidad" in desc_lower:
            code = 'VIV_LUZ'
        elif "agua" in desc_lower or "hidro" in desc_lower:
            code = 'VIV_AGUA'
        elif "internet" in desc_lower or "fibra" in desc_lower or "móvil" in desc_lower or "telefono" in desc_lower:
            code = 'VIV_TEL_INT'
        elif "curso" in desc_lower or "certific" in desc_lower:
            code = 'EDU_CURSOS'
        else:
            return None

        category = self.db.query(Category).filter(Category.code == code).first()
        return category.id if category else None

    def _resolve_emitter_category(self, emitter: str) -> str:
        """Resuelve la categoría basándose en el nombre del emisor."""
        name_lower = emitter.lower()
        if "iberdrola" in name_lower or "endesa" in name_lower or "naturgy" in name_lower:
            code = 'VIV_LUZ'
        elif "movistar" in name_lower or "vodafone" in name_lower or "orange" in name_lower or "digi" in name_lower:
            code = 'VIV_TEL_INT'
        elif "netflix" in name_lower or "spotify" in name_lower or "hbo" in name_lower:
            code = 'SUB_STREAMING'
        else:
            return None

        category = self.db.query(Category).filter(Category.code == code).first()
        return category.id if category else None
