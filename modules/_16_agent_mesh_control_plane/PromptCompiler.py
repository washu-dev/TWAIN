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

    def modifyJsonSchema(self, schema, query):
        prompt = "You will be rewriting the json schema below with the purpose of clarifying ambiguities. Your goal is to resolve any uncertainty, but do not blindly overwrite anything"
        prompt += "\n" + schema
        prompt += "+\n The following is the additional information provided by the user to resolve ambiguities"
        prompt += query
        prompt += "Your response MUST begin with '{', the first character of a json file, and end with '}', the last character of the json file"
        return prompt

    def clarificationPrompt(self, intent_spec):
        prompt = "You are to generate a list of questions for the following schema file to resolve the ambiguities. Based on the following json file, return an ordered list of specific questions whose answers will remove any uncertainty"
        prompt += intent_spec
        return prompt


