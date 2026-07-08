import sys
import os
from datetime import date, datetime

# Añadir el directorio raíz del backend al PATH para importar "app"
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, SessionLocal, Base
from app.models.user import User
from app.models.bank import BankAccount, BankTransaction
from app.models.amazon import AmazonOrder, AmazonItem
from app.models.category import Category
from app.models.accounting import AccountingEntry
from app.services.reconciliation import ReconciliationService

def run_test():
    print("=== INICIANDO SIMULACIÓN DE CONCILIACIÓN CONTABLE ===")
    
    # 1. Asegurar que las tablas existen (para desarrollo local rápido)
    print("Creando tablas si no existen...")
    Base.metadata.create_all(bind=engine)
    
    db = SessionLocal()
    try:
        # 2. Limpiar base de datos de pruebas previas
        print("Limpiando datos de prueba anteriores...")
        db.query(AccountingEntry).delete()
        db.query(AmazonItem).delete()
        db.query(AmazonOrder).delete()
        db.query(BankTransaction).delete()
        db.query(BankAccount).delete()
        db.query(User).delete()
        db.commit()

        # 3. Crear categorías semilla si están vacías
        print("Comprobando categorías semilla...")
        if db.query(Category).count() == 0:
            print("Cargando categorías básicas...")
            cats = [
                Category(name="Ocio y Estilo de Vida", code="CAT_OCI", is_system=True),
                Category(name="Tecnología y Gadgets", code="OCI_TECNOLOGIA", is_system=True),
                Category(name="Educación y Formación", code="CAT_EDU", is_system=True),
                Category(name="Libros y Material de Estudio", code="EDU_LIBROS", is_system=True),
                Category(name="Vivienda", code="CAT_VIV", is_system=True),
                Category(name="Electricidad", code="VIV_LUZ", is_system=True)
            ]
            db.add_all(cats)
            db.commit()
            
            # Asignar relaciones padre-hijo
            cat_oci = db.query(Category).filter(Category.code == "CAT_OCI").first()
            oci_tech = db.query(Category).filter(Category.code == "OCI_TECNOLOGIA").first()
            oci_tech.parent_id = cat_oci.id

            cat_edu = db.query(Category).filter(Category.code == "CAT_EDU").first()
            edu_lib = db.query(Category).filter(Category.code == "EDU_LIBROS").first()
            edu_lib.parent_id = cat_edu.id
            db.commit()

        # Obtener IDs de categorías
        cat_tech = db.query(Category).filter(Category.code == "OCI_TECNOLOGIA").first()
        cat_books = db.query(Category).filter(Category.code == "EDU_LIBROS").first()

        # 4. Crear Usuario de prueba
        print("Creando usuario de prueba...")
        test_user = User(
            email="tester@appgastos.com",
            password_hash="pbkdf2:sha256:mock_hash_value",
            base_currency="EUR"
        )
        db.add(test_user)
        db.commit()
        db.refresh(test_user)

        # 5. Crear Cuenta Bancaria de prueba
        print("Creando cuenta bancaria de prueba...")
        account = BankAccount(
            user_id=test_user.id,
            provider_name="BBVA",
            account_number_masked="**** 9876",
            account_type="checking",
            balance=2500.00
        )
        db.add(account)
        db.commit()
        db.refresh(account)

        # 6. Crear Transacción Bancaria Cruda (Amazon por €115.50)
        print("Insertando cargo bancario de Amazon...")
        bank_tx = BankTransaction(
            bank_account_id=account.id,
            transaction_date=date(2026, 7, 6),
            value_date=date(2026, 7, 6),
            raw_description="AMZN MKTP ES*1A2B3C INTERNET",
            cleaned_merchant="Amazon",
            amount=-115.50,
            currency="EUR",
            balance_snapshot=2384.50,
            import_source="CSV_MANUAL"
        )
        db.add(bank_tx)
        db.commit()
        db.refresh(bank_tx)

        # 7. Crear Pedido de Amazon correspondiente (€115.50 en la misma fecha, 3 artículos)
        print("Insertando pedido de Amazon con desgloses...")
        amazon_order = AmazonOrder(
            user_id=test_user.id,
            amazon_order_id="403-9999999-1111111",
            order_date=date(2026, 7, 6),
            total_amount=115.50,
            currency="EUR"
        )
        db.add(amazon_order)
        db.commit()
        db.refresh(amazon_order)

        # Artículos dentro del pedido de Amazon
        item1 = AmazonItem(
            amazon_order_id=amazon_order.id,
            product_title="Auriculares Inalámbricos Bluetooth Pro",
            amazon_category="Electronics",
            seller_name="Xiaomi Official Store",
            quantity=1,
            unit_price=55.50,
            total_price=55.50
        )
        item2 = AmazonItem(
            amazon_order_id=amazon_order.id,
            product_title="Libro: Designing Data-Intensive Applications",
            amazon_category="Books",
            seller_name="Amazon.es",
            quantity=1,
            unit_price=40.00,
            total_price=40.00
        )
        item3 = AmazonItem(
            amazon_order_id=amazon_order.id,
            product_title="Funda de Silicona AirPods",
            amazon_category="Electronics",
            seller_name="GadgetShop",
            quantity=1,
            unit_price=20.00,
            total_price=20.00
        )
        db.add_all([item1, item2, item3])
        db.commit()

        # 8. Ejecutar el Servicio de Conciliación
        print("\n--- Ejecutando motor de conciliación ---")
        reconciler = ReconciliationService(db)
        reconciled_count = reconciler.reconcile_user_transactions(user_id=str(test_user.id))
        print(f"Transacciones bancarias conciliadas exitosamente: {reconciled_count}\n")

        # 9. Consultar y mostrar los resultados del Libro Mayor (accounting_entries)
        print("=== RESULTADOS DEL LIBRO MAYOR (accounting_entries) ===")
        entries = db.query(AccountingEntry).filter(AccountingEntry.user_id == test_user.id).all()
        
        for idx, entry in enumerate(entries, 1):
            category = entry.category
            parent_name = category.parent.name if category.parent else "Ninguno"
            print(f"Asiento #{idx}:")
            print(f"  - Descripción: {entry.description}")
            print(f"  - Fecha:       {entry.entry_date}")
            print(f"  - Importe:     {entry.amount} EUR")
            print(f"  - Categoría:   {category.name} (Padre: {parent_name})")
            print(f"  - Tipo Conc:   {entry.reconciliation_type}")
            print(f"  - Confianza:   {entry.confidence_score}")
            print(f"  - Metadatos:   {entry.metadata_json}")
            print("-" * 50)

    except Exception as e:
        db.rollback()
        print(f"ERROR DURANTE EL TEST: {str(e)}")
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    run_test()
