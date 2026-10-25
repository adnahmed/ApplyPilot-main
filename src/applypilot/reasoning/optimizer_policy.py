"""Select public DSPy optimizers from the learning objective and available evidence.

This is a domain decision, not an optimizer queue, lock, capacity reservation or
attempt counter. The caller persists the decision with its learning-run result.
The chosen library owns the actual optimization algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Objective = Literal['demonstrations', 'bootstrap', 'instruction', 'program', 'reflective', 'minibatch']
PUBLIC_OPTIMIZERS = (
    'LabeledFewShot', 'BootstrapFewShot', 'BootstrapFewShotWithRandomSearch',
    'COPRO', 'MIPROv2', 'SIMBA', 'GEPA',
)


@dataclass(frozen=True)
class LearningObjective:
    objective: Objective
    training_examples: int
    validation_examples: int = 0
    labeled_examples: bool = True
    multiple_stages: bool = False
    allow_demonstrations: bool = True
    textual_feedback: bool = False

    def __post_init__(self):
        if not isinstance(self.objective, str) or self.objective not in {'demonstrations', 'bootstrap', 'instruction', 'program', 'reflective', 'minibatch'}:
            raise ValueError('Unknown learning objective')
        for name in ('training_examples', 'validation_examples'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f'{name} must be a nonnegative integer')
        for name in ('labeled_examples', 'multiple_stages', 'allow_demonstrations', 'textual_feedback'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be a boolean')
        if self.training_examples == 0:
            raise ValueError('Optimization requires actual training examples')


@dataclass(frozen=True)
class OptimizerChoice:
    name: str
    reason: str


def choose_optimizer(problem: LearningObjective, *, available=PUBLIC_OPTIMIZERS) -> OptimizerChoice:
    """No silent change of objective when an installed package lacks an optimizer."""
    if problem.objective == 'demonstrations':
        if not problem.allow_demonstrations or not problem.labeled_examples:
            raise ValueError('Labeled demonstration selection requires permitted, labeled examples')
        name, reason = 'LabeledFewShot', 'Select existing labeled examples without generating new labels.'
    elif problem.objective == 'bootstrap':
        if not problem.allow_demonstrations:
            raise ValueError('Bootstrapping requires permission to retain demonstrations')
        if problem.validation_examples and problem.multiple_stages:
            name, reason = 'BootstrapFewShotWithRandomSearch', 'Compare bootstrapped multi-stage demonstrations on validation evidence.'
        else:
            name, reason = 'BootstrapFewShot', 'Bootstrap demonstrations using the task metric.'
    elif problem.objective == 'reflective':
        if not problem.textual_feedback:
            raise ValueError('Reflective optimization requires meaningful textual metric feedback')
        name, reason = 'GEPA', 'Use grounded textual feedback for reflective program improvement.'
    elif problem.objective == 'minibatch':
        name, reason = 'SIMBA', 'Learn from minibatch variation in production examples.'
    elif problem.objective == 'program' or problem.multiple_stages:
        if not problem.validation_examples:
            raise ValueError('Joint program selection requires a separate validation set')
        name, reason = 'MIPROv2', 'Jointly optimize program instructions against a separate validation set.'
    else:
        name, reason = 'COPRO', 'Optimize instructions without changing demonstration policy.'
    if name not in available:
        raise ValueError(f'The installed DSPy configuration does not provide {name}')
    return OptimizerChoice(name, reason)


def installed_optimizers(dspy_module) -> tuple[str, ...]:
    return tuple(name for name in PUBLIC_OPTIMIZERS if callable(getattr(dspy_module, name, None)))
