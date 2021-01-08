# -*- coding:utf-8 -*-
__author__ = 'yangjian'
"""

"""
import copy

import numpy as np
import pandas as pd
from IPython.display import display, display_markdown
from sklearn.metrics import get_scorer
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from hypernets.experiment import Experiment
from hypernets.utils import logging
from hypernets.utils.common import isnotebook
from tabular_toolbox import dask_ex as dex
from tabular_toolbox import drift_detection as dd
from tabular_toolbox.data_cleaner import DataCleaner
from tabular_toolbox.ensemble import GreedyEnsemble
from tabular_toolbox.feature_selection import select_by_multicollinearity
from .feature_importance import feature_importance_batch

logger = logging.get_logger(__name__)

DEFAULT_EVAL_SIZE = 0.3


def _set_log_level(log_level):
    logging.set_level(log_level)

    from tabular_toolbox.utils import logging as tlogging
    tlogging.set_level(log_level)

    # import logging as pylogging
    # pylogging.basicConfig(level=log_level)


class ExperimentStep(object):
    def __init__(self, experiment, name):
        super(ExperimentStep, self).__init__()

        self.name = name
        self.experiment = experiment

        self.step_start = experiment.step_start
        self.step_end = experiment.step_end
        self.step_progress = experiment.step_progress

    @property
    def task(self):
        return self.experiment.task

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        raise NotImplemented()
        # return hyper_model, X_train, y_train, X_test, X_eval, y_eval,

    def transform(self, X, y=None, **kwargs):
        raise NotImplemented()
        # return X


class FeatureSelectStep(ExperimentStep):

    def __init__(self, experiment, name):
        super().__init__(experiment, name)

        # fitted
        self.selected_features_ = None

    def transform(self, X, y=None, **kwargs):
        if self.selected_features_ is not None:
            if logger.is_debug_enabled():
                msg = f'{self.name} transform from {len(X.columns.tolist())} to {len(self.selected_features_)} features'
                logger.debug(msg)
            X = X[self.selected_features_]
        return X


class DataCleanStep(ExperimentStep):
    def __init__(self, experiment, name, data_cleaner_args=None,
                 cv=False, train_test_split_strategy=None, random_state=None):
        super().__init__(experiment, name)

        self.data_cleaner_args = data_cleaner_args if data_cleaner_args is not None else {}
        self.cv = cv
        self.train_test_split_strategy = train_test_split_strategy
        self.random_state = random_state

        # fitted
        self.selected_features_ = None
        self.data_cleaner = None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        self.step_start('clean and split data')
        # 1. Clean Data
        if self.cv and X_eval is not None and y_eval is not None:
            X_train = pd.concat([X_train, X_eval], axis=0)
            y_train = pd.concat([y_train, y_eval], axis=0)
            X_eval = None
            y_eval = None

        data_cleaner = DataCleaner(**self.data_cleaner_args)

        X_train, y_train = data_cleaner.fit_transform(X_train, y_train)
        self.step_progress('fit_transform train set')

        if X_test is not None:
            X_test = data_cleaner.transform(X_test)
            self.step_progress('transform X_test')

        if not self.cv:
            if X_eval is None or y_eval is None:
                eval_size = kwargs.get('eval_size', DEFAULT_EVAL_SIZE)
                if self.train_test_split_strategy == 'adversarial_validation' and X_test is not None:
                    logger.debug('DriftDetector.train_test_split')
                    detector = dd.DriftDetector()
                    detector.fit(X_train, X_test)
                    X_train, X_eval, y_train, y_eval = detector.train_test_split(X_train, y_train, test_size=eval_size)
                else:
                    if self.task == 'regression' or dex.is_dask_object(X_train):
                        X_train, X_eval, y_train, y_eval = dex.train_test_split(X_train, y_train, test_size=eval_size,
                                                                                random_state=self.random_state)
                    else:
                        X_train, X_eval, y_train, y_eval = dex.train_test_split(X_train, y_train, test_size=eval_size,
                                                                                random_state=self.random_state,
                                                                                stratify=y_train)
                self.step_progress('split into train set and eval set')
            else:
                X_eval, y_eval = data_cleaner.transform(X_eval, y_eval)
                self.step_progress('transform eval set')

        self.step_end(output={'X_train.shape': X_train.shape,
                              'y_train.shape': y_train.shape,
                              'X_eval.shape': None if X_eval is None else X_eval.shape,
                              'y_eval.shape': None if y_eval is None else y_eval.shape,
                              'X_test.shape': None if X_test is None else X_test.shape})

        display_markdown('### Data Cleaner', raw=True)

        display(self.data_cleaner, display_id='output_cleaner_info1')
        display_markdown('### Train set & Eval set', raw=True)
        display(pd.DataFrame([(X_train.shape,
                               y_train.shape,
                               X_eval.shape if X_eval is not None else None,
                               y_eval.shape if y_eval is not None else None,
                               X_test.shape if X_test is not None else None)],
                             columns=['X_train.shape',
                                      'y_train.shape',
                                      'X_eval.shape',
                                      'y_eval.shape',
                                      'X_test.shape']), display_id='output_cleaner_info2')
        original_features = X_train.columns.to_list()

        self.selected_features_ = original_features
        self.data_cleaner = data_cleaner

        return X_train, y_train, X_test, X_eval, y_eval

    def transform(self, X, y=None, **kwargs):
        return self.data_cleaner.transform(X, y, **kwargs)


class SelectByMulticollinearityStep(FeatureSelectStep):

    def __init__(self, experiment, name, drop_feature_with_collinearity=True):
        super().__init__(experiment, name)

        self.drop_feature_with_collinearity = drop_feature_with_collinearity

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if self.drop_feature_with_collinearity:
            display_markdown('### Drop features with collinearity', raw=True)

            self.step_start('drop features with multicollinearity')
            corr_linkage, remained, dropped = select_by_multicollinearity(X_train)
            self.output_multi_collinearity_ = {
                'corr_linkage': corr_linkage,
                'remained': remained,
                'dropped': dropped
            }
            self.step_progress('calc correlation')

            self.selected_features_ = remained

            X_train = X_train[self.selected_features_]
            if X_eval is not None:
                X_eval = X_eval[self.selected_features_]
            if X_test is not None:
                X_test = X_test[self.selected_features_]
            self.step_progress('drop features')
            self.step_end(output=self.output_multi_collinearity_)
            # print(self.output_multi_collinearity_)
            display(
                pd.DataFrame([(k, v) for k, v in self.output_multi_collinearity_.items()], columns=['key', 'value']),
                display_id='output_drop_feature_with_collinearity')

        return X_train, y_train, X_test, X_eval, y_eval


class DriftDetectStep(FeatureSelectStep):

    def __init__(self, experiment, name, drift_detection=True):
        super().__init__(experiment, name)

        self.drift_detection = drift_detection

        # fitted
        self.output_drift_detection_ = None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        if self.drift_detection and self.experiment.X_test is not None:
            display_markdown('### Drift detection', raw=True)

            self.step_start('detect drifting')
            features, history, scores = dd.feature_selection(X_train, X_test)

            self.output_drift_detection_ = {'no_drift_features': features, 'history': history}
            self.selected_features_ = features

            X_train = X_train[self.selected_features_]
            if X_eval is not None:
                X_eval = X_eval[self.selected_features_]
            if X_test is not None:
                X_test = X_test[self.selected_features_]
            self.step_end(output=self.output_drift_detection_)

            display(pd.DataFrame((('no drift features', features), ('history', history), ('drift score', scores)),
                                 columns=['key', 'value']), display_id='output_drift_detection')

        return X_train, y_train, X_test, X_eval, y_eval


class PermutationImportanceSelectionStep(FeatureSelectStep):

    def __init__(self, experiment, name, scorer, n_est_feature_importance, importance_threshold):
        super().__init__(experiment, name)

        self.scorer = scorer
        self.n_est_feature_importance = n_est_feature_importance
        self.importance_threshold = importance_threshold

        # fixed
        self.unselected_features_ = None
        self.importances_ = None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        self.step_start('evaluate feature importance')
        display_markdown('### Evaluate feature importance', raw=True)

        best_trials = hyper_model.get_top_trails(self.n_est_feature_importance)
        estimators = []
        for trail in best_trials:
            estimators.append(hyper_model.load_estimator(trail.model_file))
        self.step_progress('load estimators')

        importances = feature_importance_batch(estimators, X_eval, y_eval, self.scorer, n_repeats=5)
        display_markdown('#### importances', raw=True)

        display(pd.DataFrame(
            zip(importances['columns'], importances['importances_mean'], importances['importances_std']),
            columns=['feature', 'importance', 'std']))
        display_markdown('#### feature selection', raw=True)

        feature_index = np.argwhere(importances.importances_mean < self.importance_threshold)
        selected_features = [feat for i, feat in enumerate(X_train.columns.to_list()) if i not in feature_index]
        unselected_features = list(set(X_train.columns.to_list()) - set(selected_features))
        self.step_progress('calc importance')

        if unselected_features:
            X_train = X_train[selected_features]
            if X_eval is not None:
                X_eval = X_eval[selected_features]
            if X_test is not None:
                X_test = X_test[selected_features]

        output_feature_importances_ = {
            'importances': importances,
            'selected_features': selected_features,
            'unselected_features': unselected_features}
        self.step_progress('drop features')
        self.step_end(output=output_feature_importances_)

        display(pd.DataFrame([('Selected', selected_features), ('Unselected', unselected_features)],
                             columns=['key', 'value']))

        self.selected_features_ = selected_features
        self.unselected_features_ = unselected_features
        self.importances_ = importances

        return X_train, y_train, X_test, X_eval, y_eval


class BaseSearchAndTrainStep(ExperimentStep):
    def __init__(self, experiment, name, scorer=None, cv=False, num_folds=3,
                 retrain_on_wholedata=False, ensemble_size=7):
        super().__init__(experiment, name)

        self.scorer = scorer if scorer is not None else get_scorer('neg_log_loss')
        self.cv = cv
        self.num_folds = num_folds
        self.retrain_on_wholedata = retrain_on_wholedata
        self.ensemble_size = ensemble_size

        # fitted
        self.estimator_ = None

    def fit_transform(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        X_train, y_train, X_test, X_eval, y_eval, searched_model = \
            self.search(hyper_model, X_train, y_train, X_test, X_eval, y_eval, **kwargs)

        estimator = self.final_train(searched_model, X_train, y_train, X_test, X_eval, y_eval, **kwargs)
        self.estimator_ = estimator

        return X_train, y_train, X_test, X_eval, y_eval

    def transform(self, X, y=None, **kwargs):
        return X

    def search(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        self.step_start('first stage search')
        display_markdown('### Pipeline search', raw=True)

        if not dex.is_dask_object(X_eval):
            kwargs['eval_set'] = (X_eval, y_eval)
        model = copy.deepcopy(hyper_model)
        model.search(X_train, y_train, X_eval, y_eval, cv=self.cv, num_folds=self.num_folds, **kwargs)

        self.step_end(output={'best_reward': model.get_best_trail().reward})

        return X_train, y_train, X_test, X_eval, y_eval, model

    def final_train(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        # 7. Ensemble
        if self.ensemble_size > 1:
            self.step_start('ensemble')
            display_markdown('### Ensemble', raw=True)

            best_trials = hyper_model.get_top_trails(self.ensemble_size)
            estimators = []
            if self.cv:
                #
                # if self.retrain_on_wholedata:
                #     display_markdown('#### retrain on whole data', raw=True)
                #     if X_eval is None or y_eval is None:
                #         stratify = y_train
                #         if self.task == 'regression':
                #             stratify = None
                #         X_train, X_eval, y_train, y_eval = train_test_split(X_train, y_train, test_size=self.eval_size,
                #                                                             random_state=self.random_state,
                #                                                             stratify=stratify)
                #     for i, trail in enumerate(best_trials):
                #         kwargs['eval_set'] = [(X_eval, y_eval)]
                #         estimator = self.hyper_model.final_train(trail.space_sample, X_train, y_train, **kwargs)
                #         estimators.append(estimator)
                oofs = None
                for i, trail in enumerate(best_trials):
                    if trail.memo.__contains__('oof'):
                        oof = trail.memo['oof']
                        if oofs is None:
                            if len(oof.shape) == 1:
                                oofs = np.zeros((oof.shape[0], len(best_trials)), dtype=np.float64)
                            else:
                                oofs = np.zeros((oof.shape[0], len(best_trials), oof.shape[-1]), dtype=np.float64)
                        oofs[:, i] = oof
                    estimators.append(hyper_model.load_estimator(trail.model_file))
                ensemble = GreedyEnsemble(self.task, estimators, scoring=self.scorer, ensemble_size=self.ensemble_size)
                print('fit on oofs')
                ensemble.fit(None, y_train, oofs)
            else:
                for trail in best_trials:
                    estimators.append(hyper_model.load_estimator(trail.model_file))
                ensemble = GreedyEnsemble(self.task, estimators, scoring=self.scorer, ensemble_size=self.ensemble_size)
                ensemble.fit(X_eval, y_eval)

            estimator = ensemble
            self.step_end(output={'ensemble': ensemble})
            display(ensemble)
        else:
            display_markdown('### Load best estimator', raw=True)

            self.step_start('load estimator')
            if self.retrain_on_wholedata:
                display_markdown('#### retrain on whole data', raw=True)
                trail = hyper_model.get_best_trail()
                X_all = pd.concat([X_train, X_eval], axis=0)
                y_all = pd.concat([y_train, y_eval], axis=0)
                estimator = hyper_model.final_train(trail.space_sample, X_all, y_all, **kwargs)
            else:
                estimator = hyper_model.load_estimator(hyper_model.get_best_trail().model_file)
            self.step_end()

        return estimator


class TwoStageSearchAndTrainStep(BaseSearchAndTrainStep):
    def __init__(self, experiment, name, scorer=None, cv=False, num_folds=3, retrain_on_wholedata=False,
                 pseudo_labeling=False, pseudo_labeling_proba_threshold=0.8,
                 ensemble_size=7, pseudo_labeling_resplit=False,
                 two_stage_importance_selection=True, n_est_feature_importance=10, importance_threshold=1e-5):
        super().__init__(experiment, name, scorer=scorer, cv=cv, num_folds=num_folds,
                         retrain_on_wholedata=retrain_on_wholedata, ensemble_size=ensemble_size)

        self.pseudo_labeling = pseudo_labeling
        self.pseudo_labeling_proba_threshold = pseudo_labeling_proba_threshold
        self.pseudo_labeling_resplit = pseudo_labeling_resplit

        if two_stage_importance_selection:
            self.pi = PermutationImportanceSelectionStep(experiment, f'{name}_pi', scorer, n_est_feature_importance,
                                                         importance_threshold)
        else:
            self.pi = None

    def transform(self, X, y=None, **kwargs):
        if self.pi:
            X = self.pi.transform(X, y, **kwargs)

        return X

    def search(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        X_train, y_train, X_test, X_eval, y_eval, searched_model = \
            super().search(hyper_model, X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval, **kwargs)

        X_pseudo, y_pseudo = \
            self.do_pseudo_label(searched_model, X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval,
                                 **kwargs)

        if self.pi:
            X_train, y_train, X_test, X_eval, y_eval = \
                self.pi.fit_transform(searched_model, X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval,
                                      **kwargs)
            selected_features, unselected_features = self.pi.selected_features_, self.pi.unselected_features_
        else:
            selected_features, unselected_features = X_train.columns.to_list(), []

        if len(unselected_features) > 0 or X_pseudo is not None:
            if self.pi and X_pseudo is not None:
                X_pseudo = X_pseudo[selected_features]
            X_train, y_train, X_test, X_eval, y_eval, searched_model = \
                self.do_two_stage_search(hyper_model, X_train, y_train, X_test, X_eval, y_eval,
                                         X_pseudo, y_pseudo, **kwargs)
        else:
            display_markdown('### Skip pipeline search stage 2', raw=True)

        return X_train, y_train, X_test, X_eval, y_eval, searched_model

    def do_pseudo_label(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None, **kwargs):
        # 5. Pseudo Labeling
        X_pseudo = None
        y_pseudo = None
        if self.task in ['binary', 'multiclass'] and self.pseudo_labeling and X_test is not None:
            es = self.ensemble_size if self.ensemble_size > 0 else 10
            best_trials = hyper_model.get_top_trails(es)
            estimators = []
            oofs = None
            for i, trail in enumerate(best_trials):
                if self.cv and trail.memo.__contains__('oof'):
                    oof = trail.memo['oof']
                    if oofs is None:
                        if len(oof.shape) == 1:
                            oofs = np.zeros((oof.shape[0], len(best_trials)), dtype=np.float64)
                        else:
                            oofs = np.zeros((oof.shape[0], len(best_trials), oof.shape[-1]), dtype=np.float64)
                    oofs[:, i] = oof
                estimators.append(hyper_model.load_estimator(trail.model_file))
            ensemble = GreedyEnsemble(self.task, estimators, scoring=self.scorer, ensemble_size=self.ensemble_size)
            if oofs is not None:
                print('fit on oofs')
                ensemble.fit(None, y_train, oofs)
            else:
                ensemble.fit(X_eval, y_eval)
            proba = ensemble.predict_proba(X_test)[:, 1]
            if self.task == 'binary':
                proba_threshold = self.pseudo_labeling_proba_threshold
                positive = np.argwhere(proba > proba_threshold).ravel()
                negative = np.argwhere(proba < 1 - proba_threshold).ravel()
                rate = 0.6
                X_test_p1 = X_test.iloc[positive]
                X_test_p2 = X_test.iloc[negative]
                y_p1 = np.ones(positive.shape, dtype='int64')
                y_p2 = np.zeros(negative.shape, dtype='int64')
                X_pseudo = pd.concat([X_test_p1, X_test_p2], axis=0)
                y_pseudo = np.concatenate([y_p1, y_p2], axis=0)
                if ensemble.classes_ is not None:
                    y_pseudo = np.array(ensemble.classes_).take(y_pseudo, axis=0)

                display_markdown('### Pseudo label set', raw=True)
                display(pd.DataFrame([(X_pseudo.shape,
                                       y_pseudo.shape,
                                       len(positive),
                                       len(negative),
                                       proba_threshold)],
                                     columns=['X_pseudo.shape',
                                              'y_pseudo.shape',
                                              'positive samples',
                                              'negative samples',
                                              'proba threshold']), display_id='output_presudo_labelings')
                try:
                    if isnotebook():
                        import seaborn as sns
                        import matplotlib.pyplot as plt
                        # Draw Plot
                        plt.figure(figsize=(8, 4), dpi=80)
                        sns.kdeplot(proba, shade=True, color="g", label="Proba", alpha=.7, bw_adjust=0.01)
                        # Decoration
                        plt.title('Density Plot of Probability', fontsize=22)
                        plt.legend()
                        plt.show()
                    else:
                        print(proba)
                except:
                    print(proba)
        return X_pseudo, y_pseudo

    def do_two_stage_search(self, hyper_model, X_train, y_train, X_test=None, X_eval=None, y_eval=None,
                            X_pseudo=None, y_pseudo=None, **kwargs):
        # 6. Final search
        self.step_start('two stage search')
        display_markdown('### Pipeline search stage 2', raw=True)

        # self.second_hyper_model = copy.deepcopy(hyper_model)
        second_hyper_model = copy.deepcopy(hyper_model)

        if not dex.is_dask_object(X_eval):
            kwargs['eval_set'] = (X_eval, y_eval)
        if X_pseudo is not None:
            if self.pseudo_labeling_resplit:
                x_list = [X_train, X_pseudo]
                y_list = [y_train, pd.Series(y_pseudo)]
                if X_eval is not None and y_eval is not None:
                    x_list.append(X_eval)
                    y_list.append(y_eval)
                X_mix = pd.concat(x_list, axis=0)
                y_mix = pd.concat(y_list, axis=0)
                if self.task == 'regression':
                    stratify = None
                else:
                    stratify = y_mix

                eval_size = kwargs.get('eval_size', 0.3)
                X_train, X_eval, y_train, y_eval = train_test_split(X_mix, y_mix, test_size=eval_size,
                                                                    random_state=self.random_state,
                                                                    stratify=stratify)
            else:
                X_train = pd.concat([X_train, X_pseudo], axis=0)
                y_train = pd.concat([y_train, pd.Series(y_pseudo)], axis=0)

            display_markdown('#### Final train set & eval set', raw=True)
            display(pd.DataFrame([(X_train.shape,
                                   y_train.shape,
                                   X_eval.shape if X_eval is not None else None,
                                   y_eval.shape if y_eval is not None else None,
                                   X_test.shape if X_test is not None else None)],
                                 columns=['X_train.shape',
                                          'y_train.shape',
                                          'X_eval.shape',
                                          'y_eval.shape',
                                          'X_test.shape']), display_id='output_cleaner_info2')

        second_hyper_model.search(X_train, y_train, X_eval, y_eval, cv=self.cv, num_folds=self.num_folds,
                                  **kwargs)

        self.step_end(output={'best_reward': second_hyper_model.get_best_trail().reward})

        return X_train, y_train, X_test, X_eval, y_eval, second_hyper_model


class SteppedExperiment(Experiment):
    def __init__(self, steps, *args, **kwargs):
        assert isinstance(steps, (tuple, list)) and all([isinstance(step, ExperimentStep) for step in steps])
        super(SteppedExperiment, self).__init__(*args, **kwargs)

        if logger.is_info_enabled():
            names = [step.name for step in steps]
            logger.info(f'create experiment with {names}')
        self.steps = steps

    def train(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        for step in self.steps:
            X_train, y_train, X_test, X_eval, y_eval = \
                step.fit_transform(hyper_model, X_train, y_train, X_test=X_test, X_eval=X_eval, y_eval=y_eval, **kwargs)

        last_step = self.steps[-1]
        assert hasattr(last_step, 'estimator_')

        pipeline_steps = [(step.name, step) for step in self.steps]
        pipeline_steps += [('estimator', self.steps[-1].estimator_)]

        return Pipeline(pipeline_steps)


class CompeteExperimentV2(SteppedExperiment):
    def __init__(self, hyper_model, X_train, y_train, X_eval=None, y_eval=None, X_test=None, eval_size=0.3,
                 train_test_split_strategy=None,
                 cv=False, num_folds=3,
                 task=None,
                 callbacks=None,
                 random_state=9527,
                 scorer=None,
                 data_cleaner_args=None,
                 drop_feature_with_collinearity=False,
                 drift_detection=True,
                 mode='one-stage',
                 two_stage_importance_selection=True,
                 n_est_feature_importance=10,
                 importance_threshold=1e-5,
                 ensemble_size=7,
                 pseudo_labeling=False,
                 pseudo_labeling_proba_threshold=0.8,
                 pseudo_labeling_resplit=False,
                 feature_generation=False,
                 retrain_on_wholedata=False,
                 log_level=None):

        steps = [DataCleanStep(self, 'data_clean',
                               data_cleaner_args=data_cleaner_args, cv=cv,
                               train_test_split_strategy=train_test_split_strategy,
                               random_state=random_state),
                 ]
        if drop_feature_with_collinearity:
            steps.append(SelectByMulticollinearityStep(self, 'select_by_multicollinearity',
                                                       drop_feature_with_collinearity=drop_feature_with_collinearity))
        if drift_detection:
            steps.append(DriftDetectStep(self, 'drift_dected', drift_detection=drift_detection))

        if mode == 'two-stage':
            last_step = TwoStageSearchAndTrainStep(self, 'two_stage_search_and_train',
                                                   scorer=scorer, cv=cv, num_folds=num_folds,
                                                   retrain_on_wholedata=retrain_on_wholedata,
                                                   pseudo_labeling=pseudo_labeling,
                                                   pseudo_labeling_proba_threshold=pseudo_labeling_proba_threshold,
                                                   ensemble_size=ensemble_size,
                                                   pseudo_labeling_resplit=pseudo_labeling_resplit,
                                                   two_stage_importance_selection=two_stage_importance_selection,
                                                   n_est_feature_importance=n_est_feature_importance,
                                                   importance_threshold=importance_threshold)
        else:
            last_step = BaseSearchAndTrainStep(self, 'base_search_and_train',
                                               scorer=scorer, cv=cv, num_folds=num_folds,
                                               ensemble_size=ensemble_size,
                                               retrain_on_wholedata=retrain_on_wholedata)
        steps.append(last_step)

        # ignore warnings
        import warnings
        warnings.filterwarnings('ignore')

        if log_level is not None:
            _set_log_level(log_level)

        super(CompeteExperimentV2, self).__init__(steps,
                                                  hyper_model, X_train, y_train, X_eval=X_eval, y_eval=y_eval,
                                                  X_test=X_test, eval_size=eval_size, task=task,
                                                  callbacks=callbacks,
                                                  random_state=random_state)

    def train(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        if isnotebook():
            import seaborn as sns
            import matplotlib.pyplot as plt
            # Draw Plot
            plt.figure(figsize=(8, 4), dpi=80)
            sns.distplot(y_train, color="g", label="y")
            # Decoration
            plt.title('Distribution of y', fontsize=16)
            plt.legend()
            plt.show()

        return super().train(hyper_model, X_train, y_train, X_test, X_eval, y_eval, **kwargs)


class CompeteExperiment(Experiment):
    def __init__(self, hyper_model, X_train, y_train, X_eval=None, y_eval=None, X_test=None, eval_size=0.3,
                 train_test_split_strategy=None,
                 cv=False, num_folds=3,
                 task=None,
                 callbacks=None,
                 random_state=9527,
                 scorer=None,
                 data_cleaner_args=None,
                 drop_feature_with_collinearity=False,
                 drift_detection=True,
                 mode='one-stage',
                 two_stage_importance_selection=True,
                 n_est_feature_importance=10,
                 importance_threshold=1e-5,
                 ensemble_size=7,
                 pseudo_labeling=False,
                 pseudo_labeling_proba_threshold=0.8,
                 pseudo_labeling_resplit=False,
                 feature_generation=False,
                 retrain_on_wholedata=False,
                 log_level=None):
        super(CompeteExperiment, self).__init__(hyper_model, X_train, y_train, X_eval=X_eval, y_eval=y_eval,
                                                X_test=X_test, eval_size=eval_size, task=task,
                                                callbacks=callbacks,
                                                random_state=random_state)
        if log_level:
            _set_log_level(log_level)

        self.data_cleaner_args = data_cleaner_args if data_cleaner_args is not None else {}
        self.drop_feature_with_collinearity = drop_feature_with_collinearity
        self.train_test_split_strategy = train_test_split_strategy
        self.cv = cv
        self.num_folds = num_folds
        self.drift_detection = drift_detection
        self.mode = mode
        self.n_est_feature_importance = n_est_feature_importance
        if scorer is None:
            self.scorer = get_scorer('neg_log_loss')
        else:
            self.scorer = scorer
        self.importance_threshold = importance_threshold
        self.two_stage_importance_selection = two_stage_importance_selection
        self.ensemble_size = ensemble_size
        self.selected_features_ = None
        self.pseudo_labeling = pseudo_labeling
        self.pseudo_labeling_proba_threshold = pseudo_labeling_proba_threshold
        self.pseudo_labeling_resplit = pseudo_labeling_resplit
        self.output_drift_detection_ = None
        self.output_multi_collinearity_ = None
        self.output_feature_importances_ = None
        # self.first_hyper_model = None
        # self.second_hyper_model = None
        # self.feature_generation = feature_generation
        self.retrain_on_wholedata = retrain_on_wholedata

    def data_split(self, X_train, y_train, X_test, X_eval=None, y_eval=None, eval_size=0.3):

        # support split data by model which trained to estimate whether a sample in train set is
        # similar with samples in test set.

        if X_eval or y_eval is None:
            X_train, X_eval, y_train, y_eval = train_test_split(X_train, y_train, test_size=eval_size,
                                                                random_state=self.random_state, stratify=y_train)
        return X_train, y_train, X_test, X_eval, y_eval

    def do_data_clean(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, eval_size=0.3, **kwargs):
        self.step_start('clean and split data')
        # 1. Clean Data
        if self.cv and X_eval is not None and y_eval is not None:
            X_train = pd.concat([X_train, X_eval], axis=0)
            y_train = pd.concat([y_train, y_eval], axis=0)
            X_eval = None
            y_eval = None

        self.data_cleaner = DataCleaner(**self.data_cleaner_args)

        X_train, y_train = self.data_cleaner.fit_transform(X_train, y_train)
        self.step_progress('fit_transform train set')

        if X_test is not None:
            X_test = self.data_cleaner.transform(X_test)
            self.step_progress('transform X_test')

        if not self.cv:
            if X_eval is None or y_eval is None:
                stratify = y_train
                if self.train_test_split_strategy == 'adversarial_validation' and X_test is not None:
                    print('DriftDetector.train_test_split')
                    detector = dd.DriftDetector()
                    detector.fit(X_train, X_test)
                    X_train, X_eval, y_train, y_eval = detector.train_test_split(X_train, y_train, test_size=eval_size)
                else:
                    if self.task == 'regression':
                        stratify = None
                    X_train, X_eval, y_train, y_eval = train_test_split(X_train, y_train, test_size=eval_size,
                                                                        random_state=self.random_state,
                                                                        stratify=stratify)
                self.step_progress('split into train set and eval set')
            else:
                X_eval, y_eval = self.data_cleaner.transform(X_eval, y_eval)
                self.step_progress('transform eval set')

        self.step_end(output={'X_train.shape': X_train.shape,
                              'y_train.shape': y_train.shape,
                              'X_eval.shape': None if X_eval is None else X_eval.shape,
                              'y_eval.shape': None if y_eval is None else y_eval.shape,
                              'X_test.shape': None if X_test is None else X_test.shape})

        display_markdown('### Data Cleaner', raw=True)

        display(self.data_cleaner, display_id='output_cleaner_info1')
        display_markdown('### Train set & Eval set', raw=True)
        display(pd.DataFrame([(X_train.shape,
                               y_train.shape,
                               X_eval.shape if X_eval is not None else None,
                               y_eval.shape if y_eval is not None else None,
                               X_test.shape if X_test is not None else None)],
                             columns=['X_train.shape',
                                      'y_train.shape',
                                      'X_eval.shape',
                                      'y_eval.shape',
                                      'X_test.shape']), display_id='output_cleaner_info2')
        original_features = X_train.columns.to_list()
        self.selected_features_ = original_features

        return X_train, y_train, X_test, X_eval, y_eval, original_features

    def do_select_by_multicollinearity(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        if self.drop_feature_with_collinearity:
            display_markdown('### Drop features with collinearity', raw=True)

            self.step_start('drop features with multicollinearity')
            corr_linkage, remained, dropped = select_by_multicollinearity(X_train)
            self.output_multi_collinearity_ = {
                'corr_linkage': corr_linkage,
                'remained': remained,
                'dropped': dropped
            }
            self.step_progress('calc correlation')

            self.selected_features_ = remained
            X_train = X_train[self.selected_features_]
            if X_eval is not None:
                X_eval = X_eval[self.selected_features_]
            if X_test is not None:
                X_test = X_test[self.selected_features_]
            self.step_progress('drop features')
            self.step_end(output=self.output_multi_collinearity_)
            # print(self.output_multi_collinearity_)
            display(
                pd.DataFrame([(k, v) for k, v in self.output_multi_collinearity_.items()], columns=['key', 'value']),
                display_id='output_drop_feature_with_collinearity')

        return X_train, y_train, X_test, X_eval, y_eval

    def do_drift_detect(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        if self.drift_detection and self.X_test is not None:
            display_markdown('### Drift detection', raw=True)

            self.step_start('detect drifting')
            features, history, scores = dd.feature_selection(X_train, X_test)

            self.output_drift_detection_ = {'no_drift_features': features, 'history': history}
            self.selected_features_ = features
            X_train = X_train[self.selected_features_]
            if X_eval is not None:
                X_eval = X_eval[self.selected_features_]
            if X_test is not None:
                X_test = X_test[self.selected_features_]
            self.step_end(output=self.output_drift_detection_)

            display(pd.DataFrame((('no drift features', features), ('history', history), ('drift score', scores)),
                                 columns=['key', 'value']), display_id='output_drift_detection')

        return X_train, y_train, X_test, X_eval, y_eval

    def do_base_search(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        first_hyper_model = copy.deepcopy(hyper_model)
        self.step_start('first stage search')
        display_markdown('### Pipeline search', raw=True)

        kwargs['eval_set'] = (X_eval, y_eval)
        first_hyper_model.search(X_train, y_train, X_eval, y_eval, cv=self.cv, num_folds=self.num_folds, **kwargs)
        self.hyper_model = first_hyper_model
        self.step_end(output={'best_reward': self.hyper_model.get_best_trail().reward})

        return X_train, y_train, X_test, X_eval, y_eval

    def do_pseudo_label(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):
        # 5. Pseudo Labeling
        X_pseudo = None
        y_pseudo = None
        if self.task in ['binary', 'multiclass'] and self.pseudo_labeling and X_test is not None:
            es = self.ensemble_size if self.ensemble_size > 0 else 10
            best_trials = self.hyper_model.get_top_trails(es)
            estimators = []
            oofs = None
            for i, trail in enumerate(best_trials):
                if self.cv and trail.memo.__contains__('oof'):
                    oof = trail.memo['oof']
                    if oofs is None:
                        if len(oof.shape) == 1:
                            oofs = np.zeros((oof.shape[0], len(best_trials)), dtype=np.float64)
                        else:
                            oofs = np.zeros((oof.shape[0], len(best_trials), oof.shape[-1]), dtype=np.float64)
                    oofs[:, i] = oof
                estimators.append(self.hyper_model.load_estimator(trail.model_file))
            ensemble = GreedyEnsemble(self.task, estimators, scoring=self.scorer, ensemble_size=self.ensemble_size)
            if oofs is not None:
                print('fit on oofs')
                ensemble.fit(None, y_train, oofs)
            else:
                ensemble.fit(X_eval, y_eval)
            proba = ensemble.predict_proba(X_test)[:, 1]
            if self.task == 'binary':
                proba_threshold = self.pseudo_labeling_proba_threshold
                positive = np.argwhere(proba > proba_threshold).ravel()
                negative = np.argwhere(proba < 1 - proba_threshold).ravel()
                rate = 0.6
                X_test_p1 = X_test.iloc[positive]
                X_test_p2 = X_test.iloc[negative]
                y_p1 = np.ones(positive.shape, dtype='int64')
                y_p2 = np.zeros(negative.shape, dtype='int64')
                X_pseudo = pd.concat([X_test_p1, X_test_p2], axis=0)
                y_pseudo = np.concatenate([y_p1, y_p2], axis=0)
                if ensemble.classes_ is not None:
                    y_pseudo = np.array(ensemble.classes_).take(y_pseudo, axis=0)

                display_markdown('### Pseudo label set', raw=True)
                display(pd.DataFrame([(X_pseudo.shape,
                                       y_pseudo.shape,
                                       len(positive),
                                       len(negative),
                                       proba_threshold)],
                                     columns=['X_pseudo.shape',
                                              'y_pseudo.shape',
                                              'positive samples',
                                              'negative samples',
                                              'proba threshold']), display_id='output_presudo_labelings')
                try:
                    if isnotebook():
                        import seaborn as sns
                        import matplotlib.pyplot as plt
                        # Draw Plot
                        plt.figure(figsize=(8, 4), dpi=80)
                        sns.kdeplot(proba, shade=True, color="g", label="Proba", alpha=.7, bw_adjust=0.01)
                        # Decoration
                        plt.title('Density Plot of Probability', fontsize=22)
                        plt.legend()
                        plt.show()
                    else:
                        print(proba)
                except:
                    print(proba)
        return X_train, y_train, X_test, X_eval, y_eval, X_pseudo, y_pseudo

    def do_two_stage_importance_selection(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None,
                                          X_pseudo=None, y_pseudo=None, **kwargs):
        unselected_features = []
        selected_features = []
        if self.two_stage_importance_selection:
            self.step_start('evaluate feature importance')
            display_markdown('### Evaluate feature importance', raw=True)

            best_trials = self.hyper_model.get_top_trails(self.n_est_feature_importance)
            estimators = []
            for trail in best_trials:
                estimators.append(self.hyper_model.load_estimator(trail.model_file))
            self.step_progress('load estimators')

            importances = feature_importance_batch(estimators, X_eval, y_eval, self.scorer,
                                                   n_repeats=5)
            display_markdown('#### importances', raw=True)

            display(pd.DataFrame(
                zip(importances['columns'], importances['importances_mean'], importances['importances_std']),
                columns=['feature', 'importance', 'std']))
            display_markdown('#### feature selection', raw=True)

            feature_index = np.argwhere(importances.importances_mean < self.importance_threshold)
            selected_features = [feat for i, feat in enumerate(X_train.columns.to_list()) if i not in feature_index]
            unselected_features = list(set(X_train.columns.to_list()) - set(selected_features))
            self.output_feature_importances_ = {
                'importances': importances,
                'selected_features': selected_features,
                'unselected_features': unselected_features}
            self.selected_features_ = selected_features
            self.step_progress('calc importance')

            X_train = X_train[self.selected_features_]
            if X_eval is not None:
                X_eval = X_eval[self.selected_features_]
            if X_test is not None:
                X_test = X_test[self.selected_features_]
            if X_pseudo is not None:
                X_pseudo = X_pseudo[self.selected_features_]
            self.step_progress('drop features')
            self.step_end(output=self.output_feature_importances_)

            display(pd.DataFrame([('Selected', selected_features), ('Unselected', unselected_features)],
                                 columns=['key', 'value']))

        return X_train, y_train, X_test, X_eval, y_eval, X_pseudo, y_pseudo, unselected_features

    def do_two_stage_search(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None,
                            X_pseudo=None, y_pseudo=None, unselected_features=None, eval_size=0.3,
                            **kwargs):
        if len(unselected_features) > 0 or X_pseudo is not None:
            # 6. Final search
            self.step_start('two stage search')
            display_markdown('### Pipeline search stage 2', raw=True)

            # self.second_hyper_model = copy.deepcopy(hyper_model)
            second_hyper_model = copy.deepcopy(hyper_model)

            kwargs['eval_set'] = (X_eval, y_eval)
            if X_pseudo is not None:
                if self.pseudo_labeling_resplit:
                    x_list = [X_train, X_pseudo]
                    y_list = [y_train, pd.Series(y_pseudo)]
                    if X_eval is not None and y_eval is not None:
                        x_list.append(X_eval)
                        y_list.append(y_eval)
                    X_mix = pd.concat(x_list, axis=0)
                    y_mix = pd.concat(y_list, axis=0)
                    if self.task == 'regression':
                        stratify = None
                    else:
                        stratify = y_mix
                    X_train, X_eval, y_train, y_eval = train_test_split(X_mix, y_mix, test_size=eval_size,
                                                                        random_state=self.random_state,
                                                                        stratify=stratify)
                else:
                    X_train = pd.concat([X_train, X_pseudo], axis=0)
                    y_train = pd.concat([y_train, pd.Series(y_pseudo)], axis=0)

                display_markdown('#### Final train set & eval set', raw=True)
                display(pd.DataFrame([(X_train.shape,
                                       y_train.shape,
                                       X_eval.shape if X_eval is not None else None,
                                       y_eval.shape if y_eval is not None else None,
                                       X_test.shape if X_test is not None else None)],
                                     columns=['X_train.shape',
                                              'y_train.shape',
                                              'X_eval.shape',
                                              'y_eval.shape',
                                              'X_test.shape']), display_id='output_cleaner_info2')

            second_hyper_model.search(X_train, y_train, X_eval, y_eval, cv=self.cv, num_folds=self.num_folds,
                                      **kwargs)
            self.hyper_model = second_hyper_model
            self.step_end(output={'best_reward': self.hyper_model.get_best_trail().reward})
        else:
            display_markdown('### Skip pipeline search stage 2', raw=True)

        return X_train, y_train, X_test, X_eval, y_eval

    def do_ensemble(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, original_features=[],
                    **kwargs):
        # 7. Ensemble
        if self.ensemble_size > 1:
            self.step_start('ensemble')
            display_markdown('### Ensemble', raw=True)

            best_trials = self.hyper_model.get_top_trails(self.ensemble_size)
            estimators = []
            if self.cv:
                #
                # if self.retrain_on_wholedata:
                #     display_markdown('#### retrain on whole data', raw=True)
                #     if X_eval is None or y_eval is None:
                #         stratify = y_train
                #         if self.task == 'regression':
                #             stratify = None
                #         X_train, X_eval, y_train, y_eval = train_test_split(X_train, y_train, test_size=self.eval_size,
                #                                                             random_state=self.random_state,
                #                                                             stratify=stratify)
                #     for i, trail in enumerate(best_trials):
                #         kwargs['eval_set'] = [(X_eval, y_eval)]
                #         estimator = self.hyper_model.final_train(trail.space_sample, X_train, y_train, **kwargs)
                #         estimators.append(estimator)
                oofs = None
                for i, trail in enumerate(best_trials):
                    if trail.memo.__contains__('oof'):
                        oof = trail.memo['oof']
                        if oofs is None:
                            if len(oof.shape) == 1:
                                oofs = np.zeros((oof.shape[0], len(best_trials)), dtype=np.float64)
                            else:
                                oofs = np.zeros((oof.shape[0], len(best_trials), oof.shape[-1]), dtype=np.float64)
                        oofs[:, i] = oof
                    estimators.append(self.hyper_model.load_estimator(trail.model_file))
                ensemble = GreedyEnsemble(self.task, estimators, scoring=self.scorer, ensemble_size=self.ensemble_size)
                print('fit on oofs')
                ensemble.fit(None, y_train, oofs)
            else:
                for trail in best_trials:
                    estimators.append(self.hyper_model.load_estimator(trail.model_file))
                ensemble = GreedyEnsemble(self.task, estimators, scoring=self.scorer, ensemble_size=self.ensemble_size)
                ensemble.fit(X_eval, y_eval)

            self.estimator = ensemble
            self.step_end(output={'ensemble': ensemble})
            display(ensemble)
        else:
            display_markdown('### Load best estimator', raw=True)

            self.step_start('load estimator')
            if self.retrain_on_wholedata:
                display_markdown('#### retrain on whole data', raw=True)
                trail = self.hyper_model.get_best_trail()
                X_all = pd.concat([X_train, X_eval], axis=0)
                y_all = pd.concat([y_train, y_eval], axis=0)
                self.estimator = self.hyper_model.final_train(trail.space_sample, X_all, y_all, **kwargs)
            else:
                self.estimator = self.hyper_model.load_estimator(self.hyper_model.get_best_trail().model_file)
            self.step_end()

        droped_features = set(original_features) - set(self.selected_features_)
        self.data_cleaner.append_drop_columns(droped_features)

        return X_train, y_train, X_test, X_eval, y_eval

    def do_xxx(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, **kwargs):

        return X_train, y_train, X_test, X_eval, y_eval

    def train(self, hyper_model, X_train, y_train, X_test, X_eval=None, y_eval=None, eval_size=0.3, **kwargs):
        """Run an experiment
        """
        # ignore warnings
        import warnings
        warnings.filterwarnings('ignore')

        display_markdown('### Input Data', raw=True)
        display(pd.DataFrame([(X_train.shape,
                               y_train.shape,
                               X_eval.shape if X_eval is not None else None,
                               y_eval.shape if y_eval is not None else None,
                               X_test.shape if X_test is not None else None)],
                             columns=['X_train.shape',
                                      'y_train.shape',
                                      'X_eval.shape',
                                      'y_eval.shape',
                                      'X_test.shape']), display_id='output_intput')

        if isnotebook():
            import seaborn as sns
            import matplotlib.pyplot as plt
            # Draw Plot
            plt.figure(figsize=(8, 4), dpi=80)
            sns.distplot(y_train, color="g", label="y")
            # Decoration
            plt.title('Distribution of y', fontsize=16)
            plt.legend()
            plt.show()

        # 1. Clean Data
        X_train, y_train, X_test, X_eval, y_eval, original_features = \
            self.do_data_clean(hyper_model, X_train, y_train, X_test, X_eval, y_eval)
        # print('-' * 20, 'do_data_clean done')

        # 2. Drop features with multicollinearity
        X_train, y_train, X_test, X_eval, y_eval = \
            self.do_select_by_multicollinearity(hyper_model, X_train, y_train, X_test, X_eval, y_eval)
        # print('-' * 20, 'do_select_by_multicollinearity done')

        # 3. Shift detection
        X_train, y_train, X_test, X_eval, y_eval = \
            self.do_drift_detect(hyper_model, X_train, y_train, X_test, X_eval, y_eval)
        # print('-' * 20, 'do_drift_detect done')

        # 4. Baseline search
        X_train, y_train, X_test, X_eval, y_eval = \
            self.do_base_search(hyper_model, X_train, y_train, X_test, X_eval, y_eval, **kwargs)
        # print('-' * 20, 'do_base_search done')

        if self.mode == 'two-stage':
            # # 5. Pseudo Labeling
            X_train, y_train, X_test, X_eval, y_eval, X_pseudo, y_pseudo = \
                self.do_pseudo_label(hyper_model, X_train, y_train, X_test, X_eval, y_eval)
            # print('-' * 20, 'do_pseudo_label done')
            # # 5. Feature importance evaluation
            X_train, y_train, X_test, X_eval, y_eval, X_pseudo, y_pseudo, unselected_features = \
                self.do_two_stage_importance_selection(hyper_model, X_train, y_train, X_test, X_eval, y_eval,
                                                       X_pseudo, y_pseudo, **kwargs)
            # print('-' * 20, 'do_two_stage_importance_selection done')

            # if len(unselected_features) > 0 or X_pseudo is not None:
            #     # 6. Final search
            # ...
            # else:
            #     display_markdown('### Skip pipeline search stage 2', raw=True)
            X_train, y_train, X_test, X_eval, y_eval = \
                self.do_two_stage_search(hyper_model, X_train, y_train, X_test, X_eval, y_eval, X_pseudo, y_pseudo,
                                         unselected_features, **kwargs)
            # print('-' * 20, 'do_two_stage_search done')

        # # 7. Ensemble
        X_train, y_train, X_test, X_eval, y_eval = \
            self.do_ensemble(hyper_model, X_train, y_train, X_test, X_eval, y_eval, original_features)
        # print('-' * 20, 'do_ensemble done')

        # 8. Compose pipeline
        self.step_start('compose pipeline')

        display_markdown('### Compose pipeline', raw=True)

        pipeline = Pipeline([('data_cleaner', self.data_cleaner), ('estimator', self.estimator)])
        self.step_end()
        print(pipeline)
        display_markdown('### Finished', raw=True)
        # 9. Save model
        return pipeline
