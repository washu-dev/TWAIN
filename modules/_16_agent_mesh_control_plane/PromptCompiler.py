class PromptCompiler:
    # Setup
    def __init__(self):
        self.prompts = []
        self.userPrompt = ""

    # PROMPT INPUTS
    def promptFromFile(self, file):
        with open(f"{file}", "r") as file:
            self.prompts.append(file.read())
        return self

    def promptFromText(self, text):
        self.prompts.append(text)
        return self

    def setUserPrompt(self, userPrompt):
        self.userPrompt = userPrompt
        return self

    # Variable Modification
    def getPrompt(self):
        return "\n".join(self.prompts)

    def resetPrompt(self):
        self.prompts = []
        return self

    def resetUserPrompt(self):  # Technically redundant but felt like it balanced resetPrompt
        self.userPrompt = ""
        return self

    # SHORTCUTS
    def dataPrompt(self, subject):
        self.resetPrompt()
        self.promptFromFile("../../Intelligence Layer/Restraints.txt")
        self.promptFromFile(f"../Schema/{subject[:1].upper()}{subject[1:].lower()}Schema.json")
        self.promptFromText(self.userPrompt)
        return self.getPrompt()

    def subjectPrompt(self):
        self.resetPrompt()
        self.promptFromFile("../../Intelligence Layer/SubjectPrompt.txt")
        self.promptFromText(self.userPrompt)
        return self.getPrompt()


class PromptGenerator:
    def __init__(self):
        self.pComp = PromptCompiler()

    def jsonSchemaPrompt(self, schema, query):
        prompt = "You will be generating a json file to answer the following question:"
        prompt += query
        prompt += "The json file must align exactly with the following json file"
        with open(f"{schema}", "r") as f:
            prompt += f.read()
        prompt += "Your response MUST begin with '{', the first character of a json file, and end with '}', the last character of the json file"

        return prompt
