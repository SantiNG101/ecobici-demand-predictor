import pytest


@pytest.fixture(autouse=True, scope="session")
def registro_aparte(tmp_path_factory):
    """Los pedidos de los tests no van al registro real."""
    carpeta = tmp_path_factory.mktemp("logs")
    with pytest.MonkeyPatch.context() as parche:
        parche.setenv("ECOBICI_LOGS_DIR", str(carpeta))
        yield carpeta
