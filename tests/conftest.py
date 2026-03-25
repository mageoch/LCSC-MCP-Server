import pytest
from lcsc_mcp.db import PartsDB


@pytest.fixture
def env_vars(monkeypatch):
    """Set required JLCPCB API credentials in environment."""
    monkeypatch.setenv("JLCPCB_APP_ID", "test_app_id")
    monkeypatch.setenv("JLCPCB_API_KEY", "test_api_key")
    monkeypatch.setenv("JLCPCB_API_SECRET", "test_secret")


@pytest.fixture
def mem_db():
    """In-memory PartsDB — no disk I/O."""
    db = PartsDB(":memory:")
    yield db
    db.close()


@pytest.fixture
def sample_parts():
    """Resistor (Basic), capacitor (Basic), inductor (Extended), IC (Extended), excluded cable."""
    return [
        {
            "lcscPart": "C25744",
            "firstCategory": "Resistors",
            "secondCategory": "Chip Resistor - Surface Mount",
            "mfrPart": "0402WGF1002TCE",
            "package": "0402",
            "solderJoint": 2,
            "manufacturer": "UNI-ROYAL",
            "libraryType": "base",
            "description": "10kΩ ±1% 1/16W",
            "datasheet": "https://example.com/ds.pdf",
            "stock": 1000000,
            "price": "20-100:0.001,100-1000:0.0008",
        },
        {
            "lcscPart": "C1525",
            "firstCategory": "Capacitors",
            "secondCategory": "Multilayer Ceramic Capacitors MLCC - SMD/SMT",
            "mfrPart": "CL05B104KO5NNNC",
            "package": "0402",
            "solderJoint": 2,
            "manufacturer": "Samsung",
            "libraryType": "base",
            "description": "100nF ±10% 25V X7R",
            "datasheet": "",
            "stock": 5000000,
            "price": "20-100:0.002",
        },
        {
            "lcscPart": "C1044",
            "firstCategory": "Inductors/Coils/Transformers",
            "secondCategory": "Inductors (SMD)",
            "mfrPart": "LQG15HS10NJ02D",
            "package": "0402",
            "solderJoint": 2,
            "manufacturer": "Murata",
            "libraryType": "extend",
            "description": "10nH ±5% 100mA",
            "datasheet": "",
            "stock": 100000,
            "price": "100-1000:0.05",
        },
        {
            "lcscPart": "C20734",
            "firstCategory": "Integrated Circuits",
            "secondCategory": "Microcontrollers",
            "mfrPart": "STM32F103C8T6",
            "package": "LQFP-48",
            "solderJoint": 48,
            "manufacturer": "STMicroelectronics",
            "libraryType": "extend",
            "description": "32-bit MCU",
            "datasheet": "",
            "stock": 50000,
            "price": "1-10:2.50",
        },
        {
            "lcscPart": "C99999",
            "firstCategory": "Wire/Cable/DataCable",
            "secondCategory": "USB Cable",
            "mfrPart": "USB-A-1M",
            "package": "",
            "solderJoint": 0,
            "manufacturer": "Generic",
            "libraryType": "base",
            "description": "1m USB cable",
            "datasheet": "",
            "stock": 100,
            "price": "1-10:1.0",
        },
    ]
