import subprocess
import sys
import json
from jsonschema import validate
from jsonschema.exceptions import ValidationError, SchemaError


class SchemaValidator:
    def __init__(self):
        pass
    def validateFromString(self, schemaString, jsonString):
        try:
            schemaData = json.loads(schemaString)[0]
            jsonData = json.loads(jsonString)[0]
            validate(instance=jsonData, schema=schemaData)
            print("Validated schemas")
            return True
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
            return False
        except ValidationError as e:
            print(f"\"{jsonString}\" fails againt schema: {e.message}")
            return False
        except SchemaError as e:
            print(f"JSON file does not match schema: {e} ")
            return False
    def validateFromPath(self, schemaFile, jsonFile):
        try:
            with open(schemaFile, "r") as _schema:
                schemaData = json.load(_schema)
            with open(jsonFile) as _jsonFile:
                jsonData = json.load(_jsonFile)
            validate(instance=jsonData, schema=schemaData)
            print("Validated Schema")
            return True
        except FileNotFoundError as e:
            print(f"Could not find file: Filename -- {e}")
            return False
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
            return False
        except ValidationError as e:
            print(f"\"{jsonFile}\" fails againt schema: {e.message}")
            return False
        except SchemaError as e:
            print(f"JSON file does not match schema: {e} ")
            return False

if __name__ == "__main__":
    schema = SchemaValidator()
    schema.validateFromPath("test.json", "test.json")