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
    def promptFromText(self,text):
        self.prompts.append(text)
        return self
    def setUserPrompt(self,userPrompt):
        self.userPrompt = userPrompt
        return self

    # Variable Modification
    def getPrompt(self):
        return "\n".join(self.prompts)
    def resetPrompt(self):
        self.prompts = []
        return self
    def resetUserPrompt(self): # Technically redundant but felt like it balanced resetPrompt
        self.userPrompt = ""
        return self

    # SHORTCUTS
    def dataPrompt(self, subject):
        self.resetPrompt()
        self.promptFromFile("Restraints.txt")
        self.promptFromFile(f"../Schema/{subject[:1].upper()}{subject[1:].lower()}Schema.json")
        return self.getPrompt()

    def subjectPrompt(self):
        self.promptFromFile("SubjectPrompt.txt")
        return self.getPrompt()